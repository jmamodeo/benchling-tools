from __future__ import annotations

import io
import json
import logging
import os
import shlex
import stat

import httpx
import keyring
import pytest
from conftest import SANDBOX_SECRET, configure_env, plain_stdout, runtime_for
from fake_benchling import ENTRY_ID, TEXT_PART, FakeBenchling
from memory_keyring import MemoryKeyring
from typer.testing import CliRunner

from benchling_tools import cli
from benchling_tools.credentials import build_environment, login, logout
from benchling_tools.environment import EnvironmentConfigStore
from benchling_tools.errors import AuthenticationError, ValidationError
from benchling_tools.keychain import Keychain, keychain_message
from benchling_tools.models import Envelope
from benchling_tools.redact import redact, register_secret

SECRET = "sensitive-test-secret"
TOKEN = "memory-only-access-token"


def token_transport(fake: FakeBenchling) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(fake.handler))


def patch_token_client(monkeypatch, fake: FakeBenchling) -> None:
    monkeypatch.setattr(
        "benchling_tools.credentials.open_token_client",
        lambda: token_transport(fake),
    )


def test_login_saves_only_after_the_token_request_and_hides_the_secret(monkeypatch) -> None:
    fake = FakeBenchling()
    fake.access_token = TOKEN
    patch_token_client(monkeypatch, fake)
    monkeypatch.setattr(
        "benchling_tools.cli.getpass.getpass",
        lambda prompt="": (_ for _ in ()).throw(AssertionError("getpass")),
    )
    monkeypatch.setattr(
        "benchling_tools.cli._read_terminal",
        lambda label: (_ for _ in ()).throw(AssertionError("tty")),
    )
    result = CliRunner().invoke(
        cli.app,
        [
            "auth",
            "login",
            "--env",
            "dev",
            "--tenant-url",
            "https://your-tenant.benchling.com",
            "--client-id",
            "dev-client",
            "--secret-stdin",
        ],
        input=SECRET,
    )
    assert result.exit_code == 0, result.stdout
    assert SECRET not in result.stdout
    assert SECRET not in result.stderr
    assert TOKEN not in result.stdout
    payload = json.loads(result.stdout)["data"]
    assert payload["saved"] is True
    assert payload["credential_source"] == "keyring"
    assert payload["client_id"] == "dev-client"
    store = EnvironmentConfigStore()
    text = store.environments_path.read_text()
    assert SECRET not in text and TOKEN not in text
    assert stat.S_IMODE(store.environments_path.stat().st_mode) == 0o600
    assert Keychain().get("dev") == SECRET
    assert fake.calls[0].path == "/oauth/token"
    assert fake.calls[0].body == {"grant_type": "client_credentials", "client_id": "dev-client"}


def test_failed_token_request_saves_nothing_and_hides_the_response(monkeypatch, caplog) -> None:
    caplog.set_level(logging.DEBUG)
    fake = FakeBenchling()
    fake.token_status = 401
    fake.token_body = {"error": SECRET, "client_secret": SECRET, "access_token": TOKEN}
    patch_token_client(monkeypatch, fake)
    seen: list[str] = []
    monkeypatch.setattr(
        "benchling_tools.cli.getpass.getpass",
        lambda prompt="": seen.append("prompted") or SECRET,
    )
    result = CliRunner().invoke(
        cli.app,
        [
            "auth",
            "login",
            "--env",
            "dev",
            "--tenant-url",
            "https://your-tenant.benchling.com",
            "--client-id",
            "dev-client",
            "--secret-stdin",
        ],
        input=SECRET,
    )
    assert result.exit_code == 3
    assert SECRET not in result.stdout
    assert TOKEN not in result.stdout
    assert SECRET not in caplog.text
    error = json.loads(result.stdout)["error"]
    assert error["code"] == "authentication_error"
    assert error["message"] == (
        "Benchling at https://your-tenant.benchling.com rejected the client ID or secret."
    )
    assert error["details"]["env"] == "dev"
    assert error["details"]["tenant_url"] == "https://your-tenant.benchling.com"
    assert error["details"]["status"] == 401
    assert not EnvironmentConfigStore().environments_path.exists()
    assert Keychain().get("dev") is None
    assert seen == []


def test_invalid_url_is_rejected_before_the_secret_is_read(monkeypatch) -> None:
    def forbidden(prompt=""):
        raise AssertionError("secret prompt")

    monkeypatch.setattr("benchling_tools.cli.getpass.getpass", forbidden)
    monkeypatch.setattr(
        "benchling_tools.cli._read_terminal",
        lambda label: (_ for _ in ()).throw(AssertionError("tty")),
    )
    result = CliRunner().invoke(
        cli.app,
        [
            "auth",
            "login",
            "--env",
            "dev",
            "--tenant-url",
            "https://user:sensitive-test-secret@your-tenant.benchling.com",
            "--client-id",
            "dev-client",
            "--secret-stdin",
        ],
        input=SECRET,
    )
    assert result.exit_code == 2
    assert SECRET not in result.stdout
    assert "sensitive-test-secret" not in result.stdout
    assert "user and password" in json.loads(result.stdout)["error"]["message"]
    assert not EnvironmentConfigStore().environments_path.exists()


def test_secret_stdin_and_terminal_prompts(monkeypatch) -> None:
    fake = FakeBenchling()
    patch_token_client(monkeypatch, fake)
    answers = iter(["https://your-tenant.benchling.com", "stdin-client"])
    monkeypatch.setattr("benchling_tools.cli._prompts_available", lambda **kwargs: True)
    monkeypatch.setattr("benchling_tools.cli._read_terminal", lambda label: next(answers))
    result = CliRunner().invoke(
        cli.app,
        ["auth", "login", "--env", "lab", "--secret-stdin"],
        input=SECRET,
    )
    assert result.exit_code == 0, result.stdout
    assert SECRET not in result.stdout
    assert Keychain().get("lab") == SECRET
    record = json.loads(EnvironmentConfigStore().environments_path.read_text())
    assert record["environments"]["lab"]["client_id"] == "stdin-client"


def test_secret_is_not_accepted_as_an_argument(monkeypatch) -> None:
    monkeypatch.setattr(
        "benchling_tools.cli.getpass.getpass",
        lambda prompt="": (_ for _ in ()).throw(AssertionError("prompted")),
    )
    result = CliRunner().invoke(
        cli.app,
        ["auth", "login", "--env", "dev", "--client-secret", SECRET],
    )
    assert result.exit_code != 0
    assert not EnvironmentConfigStore().environments_path.exists()
    help_text = plain_stdout(["auth", "login", "--help"])
    assert "--secret-stdin" in help_text
    assert "--tenant-url" in help_text
    assert "--client-id" in help_text
    assert "--client-secret" not in help_text


def test_missing_keyring_fails_before_prompting_and_names_a_fix(monkeypatch) -> None:
    keyring.set_keyring(keyring.backends.fail.Keyring())
    monkeypatch.setattr("benchling_tools.keychain.provider_installed", lambda: False)
    monkeypatch.setattr("benchling_tools.keychain.secret_service_reachable", lambda: False)
    monkeypatch.setattr(
        "benchling_tools.cli.getpass.getpass",
        lambda prompt="": (_ for _ in ()).throw(AssertionError("prompted")),
    )
    result = CliRunner().invoke(cli.app, ["auth", "login", "--env", "dev"])
    assert result.exit_code == 3
    error = json.loads(result.stdout)["error"]
    message = error["message"]
    assert error["details"]["cause"] == "no_backend"
    assert message == (
        "No usable OS keychain is available because no Secret Service backend is installed.\n"
        "sudo apt-get install -y gnome-keyring dbus\n"
        "See the README section Headless Linux."
    )
    assert "HOME, LOGNAME" not in message
    assert not EnvironmentConfigStore().environments_path.exists()


def _dbus_keychain_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    keyring.set_keyring(keyring.backends.fail.Keyring())
    monkeypatch.setattr("benchling_tools.keychain.sys.platform", "linux")
    monkeypatch.setattr("benchling_tools.keychain.provider_installed", lambda: True)
    monkeypatch.setattr("benchling_tools.keychain.secret_service_reachable", lambda: False)


def _login_fix_args(message: str) -> list[str]:
    """Return the argv of the login pipeline inside the printed dbus command."""
    command = next(line for line in message.splitlines() if line.startswith("dbus-run-session"))
    outer = shlex.split(command)
    assert outer[:4] == ["dbus-run-session", "--", "sh", "-c"]
    return shlex.split(outer[4])


def _flag(argv: list[str], name: str) -> str:
    return argv[argv.index(name) + 1]


def test_login_keychain_error_fills_in_the_supplied_values(monkeypatch: pytest.MonkeyPatch) -> None:
    _dbus_keychain_failure(monkeypatch)
    result = CliRunner().invoke(
        cli.app,
        [
            "auth",
            "login",
            "--env",
            "lab",
            "--tenant-url",
            "your-tenant.benchling.com/api/v3",
            "--client-id",
            "app-client",
            "--secret-stdin",
        ],
        input=SECRET,
    )
    assert result.exit_code == 3
    message = json.loads(result.stdout)["error"]["message"]
    args = _login_fix_args(message)
    assert _flag(args, "--env") == "lab"
    assert _flag(args, "--tenant-url") == "https://your-tenant.benchling.com"
    assert _flag(args, "--client-id") == "app-client"
    assert "$KEYRING_PASSWORD" in message
    assert "$BENCHLING_CLIENT_SECRET" in message
    assert SECRET not in message
    assert "--client-id CLIENT" not in message


def test_login_keychain_error_keeps_placeholders_for_omitted_flags(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _dbus_keychain_failure(monkeypatch)
    monkeypatch.setattr(
        "benchling_tools.cli.getpass.getpass",
        lambda prompt="": (_ for _ in ()).throw(AssertionError("prompted")),
    )
    result = CliRunner().invoke(
        cli.app,
        ["auth", "login", "--env", "lab", "--client-id", "app-client"],
    )
    assert result.exit_code == 3
    args = _login_fix_args(json.loads(result.stdout)["error"]["message"])
    assert _flag(args, "--env") == "lab"
    assert _flag(args, "--tenant-url") == "https://your-tenant.benchling.com"
    assert _flag(args, "--client-id") == "app-client"

    invalid = CliRunner().invoke(
        cli.app,
        [
            "auth",
            "login",
            "--env",
            "lab",
            "--tenant-url",
            "http://your-tenant.benchling.com",
            "--client-id",
            "app-client",
            "--secret-stdin",
        ],
        input=SECRET,
    )
    assert invalid.exit_code == 3
    invalid_message = json.loads(invalid.stdout)["error"]["message"]
    invalid_args = _login_fix_args(invalid_message)
    assert _flag(invalid_args, "--tenant-url") == "https://your-tenant.benchling.com"
    assert "http://" not in invalid_message
    assert SECRET not in invalid_message


def test_login_keychain_error_uses_a_saved_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    configure_env("lab", "https://other-tenant.benchling.com/api/v2/", "saved-client", secret=None)
    _dbus_keychain_failure(monkeypatch)
    monkeypatch.setattr(
        "benchling_tools.cli.getpass.getpass",
        lambda prompt="": (_ for _ in ()).throw(AssertionError("prompted")),
    )
    result = CliRunner().invoke(cli.app, ["auth", "login", "--env", "lab"])
    assert result.exit_code == 3
    args = _login_fix_args(json.loads(result.stdout)["error"]["message"])
    assert _flag(args, "--env") == "lab"
    assert _flag(args, "--tenant-url") == "https://other-tenant.benchling.com"
    assert _flag(args, "--client-id") == "saved-client"

    overridden = CliRunner().invoke(
        cli.app,
        [
            "auth",
            "login",
            "--env",
            "lab",
            "--tenant-url",
            "https://your-tenant.benchling.com/",
            "--client-id",
            "app-client",
            "--secret-stdin",
        ],
        input=SECRET,
    )
    overridden_args = _login_fix_args(json.loads(overridden.stdout)["error"]["message"])
    assert _flag(overridden_args, "--tenant-url") == "https://your-tenant.benchling.com"
    assert _flag(overridden_args, "--client-id") == "app-client"
    assert SECRET not in overridden.stdout


def test_login_keychain_error_does_not_copy_a_different_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    configure_env("lab", "https://other-tenant.benchling.com/", "saved-client", secret=None)
    _dbus_keychain_failure(monkeypatch)
    result = CliRunner().invoke(
        cli.app,
        ["auth", "login", "--env", "demo", "--client-id", "app-client"],
    )
    assert result.exit_code == 3
    message = json.loads(result.stdout)["error"]["message"]
    args = _login_fix_args(message)
    assert _flag(args, "--env") == "demo"
    assert _flag(args, "--tenant-url") == "https://your-tenant.benchling.com"
    assert _flag(args, "--client-id") == "app-client"
    assert "saved-client" not in message
    assert "other-tenant" not in message


def test_login_keychain_error_quotes_a_client_id(monkeypatch: pytest.MonkeyPatch) -> None:
    _dbus_keychain_failure(monkeypatch)
    client_id = "app'id"
    result = CliRunner().invoke(
        cli.app,
        [
            "auth",
            "login",
            "--env",
            "lab",
            "--tenant-url",
            "https://your-tenant.benchling.com",
            "--client-id",
            client_id,
            "--secret-stdin",
        ],
        input=SECRET,
    )
    assert result.exit_code == 3
    message = json.loads(result.stdout)["error"]["message"]
    # The id is quoted inside the script, and the script is quoted for sh -c.
    # Splitting both layers yields the original value and does not run it.
    assert _flag(_login_fix_args(message), "--client-id") == client_id
    assert "--client-id app'id" not in message
    assert "$BENCHLING_CLIENT_SECRET" in message
    assert SECRET not in message


def test_keychain_error_outside_login_keeps_placeholders(monkeypatch: pytest.MonkeyPatch) -> None:
    configure_env("lab", "https://other-tenant.benchling.com/", "saved-client", secret=None)
    _dbus_keychain_failure(monkeypatch)
    result = CliRunner().invoke(cli.app, ["auth", "status"])
    assert result.exit_code == 3
    message = json.loads(result.stdout)["error"]["message"]
    args = _login_fix_args(message)
    assert _flag(args, "--env") == "dev"
    assert _flag(args, "--tenant-url") == "https://your-tenant.benchling.com"
    assert _flag(args, "--client-id") == "CLIENT"
    assert "saved-client" not in message
    assert "other-tenant" not in message

    from benchling_tools.keychain import dbus_login_command

    placeholder = dbus_login_command()
    assert "--env dev " in placeholder
    assert "--client-id CLIENT " in placeholder
    assert shlex.split(shlex.split(placeholder)[4])  # inner script parses


def test_login_rolls_back_when_the_file_cannot_be_saved(monkeypatch) -> None:
    configure_env(
        "dev", "https://your-tenant.benchling.com/", "old-client", "previous-secret-value"
    )
    fake = FakeBenchling()

    def boom(self, environment):
        raise OSError("disk full")

    monkeypatch.setattr(EnvironmentConfigStore, "upsert", boom)
    with pytest.raises(AuthenticationError) as caught:
        login(
            EnvironmentConfigStore(),
            Keychain(),
            name="dev",
            tenant_url="https://other-tenant.benchling.com/",
            client_id="new-client",
            secret=SECRET,
            http=token_transport(fake),
        )
    assert SECRET not in json.dumps(caught.value.details)
    assert Keychain().get("dev") == "previous-secret-value"
    saved = json.loads(EnvironmentConfigStore().environments_path.read_text())
    assert saved["environments"]["dev"]["client_id"] == "old-client"


def test_logout_removes_the_record_and_the_secret() -> None:
    configure_env("dev", "https://your-tenant.benchling.com/", secret=SECRET)
    result = CliRunner().invoke(cli.app, ["auth", "logout", "--env", "dev"])
    assert result.exit_code == 0, result.stdout
    assert SECRET not in result.stdout
    assert json.loads(result.stdout)["data"] == {"env": "dev", "removed": True}
    saved = json.loads(EnvironmentConfigStore().environments_path.read_text())
    assert saved["environments"] == {}
    assert Keychain().get("dev") is None


def test_logout_removes_an_orphaned_secret_and_rejects_unknown_names() -> None:
    Keychain().set("dev", SECRET)
    assert EnvironmentConfigStore().remove("dev") is False
    logout(EnvironmentConfigStore(), Keychain(), "dev")
    assert Keychain().get("dev") is None
    with pytest.raises(ValidationError, match="not configured"):
        logout(EnvironmentConfigStore(), Keychain(), "missing")
    with pytest.raises(ValidationError, match="lowercase letters and digits"):
        logout(EnvironmentConfigStore(), Keychain(), "DEV")


def test_status_check_reports_token_success_without_printing_it(monkeypatch) -> None:
    configure_env("dev", "https://your-tenant.benchling.com/", "dev-client", SECRET)
    fake = FakeBenchling()
    fake.access_token = TOKEN
    patch_token_client(monkeypatch, fake)
    result = CliRunner().invoke(cli.app, ["auth", "status", "--check"])
    assert result.exit_code == 0, result.stdout
    row = json.loads(result.stdout)["data"]["environments"]["dev"]
    assert row["token_check"] == {"ok": True}
    assert row["secret_present"] is True
    assert TOKEN not in result.stdout
    assert SECRET not in result.stdout
    home = EnvironmentConfigStore().root
    dumped = "\n".join(path.read_text() for path in home.rglob("*") if path.is_file())
    assert TOKEN not in dumped
    assert SECRET not in dumped


def test_status_check_failure_is_an_authentication_error_without_the_secret(monkeypatch) -> None:
    configure_env("dev", "https://your-tenant.benchling.com/", secret=SECRET)
    fake = FakeBenchling()
    fake.token_status = 401
    fake.token_body = {"error": SECRET}
    patch_token_client(monkeypatch, fake)
    result = CliRunner().invoke(cli.app, ["auth", "status", "--check", "--env", "dev"])
    assert result.exit_code == 3
    payload = json.loads(result.stdout)
    assert payload["error"]["details"]["failed_envs"] == ["dev"]
    assert payload["error"]["details"]["environments"]["dev"]["token_check"]["ok"] is False
    assert payload["error"]["message"] == (
        "Benchling at https://your-tenant.benchling.com rejected the client ID or secret."
    )
    assert SECRET not in result.stdout


def test_commands_fail_clearly_when_the_keychain_disappears() -> None:
    from benchling_tools.mcp_server import run_mcp_server
    from benchling_tools.runtime import Runtime

    configure_env("dev", "https://your-tenant.benchling.com/", secret=SECRET)
    keyring.set_keyring(keyring.backends.fail.Keyring())
    with pytest.raises(AuthenticationError, match="keychain"):
        Runtime("dev")
    with pytest.raises(AuthenticationError, match="keychain"):
        run_mcp_server()


def test_status_without_environments_does_not_require_a_keyring() -> None:
    keyring.set_keyring(keyring.backends.fail.Keyring())
    result = CliRunner().invoke(cli.app, ["auth", "status"])
    assert result.exit_code == 0
    assert json.loads(result.stdout)["data"]["environments"] == {}


def test_keychain_rejects_unusable_backends_and_hides_backend_errors(monkeypatch) -> None:
    class PlaintextKeyring(MemoryKeyring):
        pass

    class NullKeyring(MemoryKeyring):
        pass

    NullKeyring.__module__ = "keyring.backends.null"
    keyring.set_keyring(PlaintextKeyring())
    monkeypatch.setattr("benchling_tools.keychain.provider_installed", lambda: False)
    monkeypatch.setattr("benchling_tools.keychain.secret_service_reachable", lambda: False)
    with pytest.raises(AuthenticationError) as plaintext:
        Keychain().require()
    assert plaintext.value.details["cause"] == "no_backend"
    assert "Secret Service backend is installed" in plaintext.value.message
    assert SECRET not in plaintext.value.message

    keyring.set_keyring(NullKeyring())
    with pytest.raises(AuthenticationError):
        Keychain().set("dev", SECRET)

    class Broken(MemoryKeyring):
        @property
        def priority(self) -> float:
            raise RuntimeError("unavailable")

    keyring.set_keyring(Broken())
    with pytest.raises(AuthenticationError):
        Keychain().require()

    class Exploding(MemoryKeyring):
        def get_password(self, service: str, username: str) -> str | None:
            raise keyring.errors.KeyringLocked("locked")

        def set_password(self, service: str, username: str, password: str) -> None:
            raise keyring.errors.KeyringError(password)

        def delete_password(self, service: str, username: str) -> None:
            raise keyring.errors.InitError("init")

    keyring.set_keyring(Exploding())
    chain = Keychain()
    with pytest.raises(AuthenticationError) as read_error:
        chain.get("dev")
    assert SECRET not in json.dumps(read_error.value.details)
    with pytest.raises(AuthenticationError) as write_error:
        chain.set("dev", SECRET)
    assert SECRET not in json.dumps(write_error.value.details)
    assert SECRET not in write_error.value.message
    with pytest.raises(AuthenticationError):
        chain.delete("dev")


def test_non_string_keychain_values_are_ignored() -> None:
    class Weird(MemoryKeyring):
        def get_password(self, service: str, username: str):
            return 5

    assert Keychain(Weird()).get("dev") is None


def test_terminal_prompts_and_empty_secret(monkeypatch) -> None:
    class FakeTerminal:
        def __init__(self, name: str, mode: str = "r", encoding: str | None = None) -> None:
            self.name = name

        def write(self, text: str) -> None:
            return None

        def flush(self) -> None:
            return None

        def readline(self) -> str:
            return "https://your-tenant.benchling.com\n"

        def __enter__(self) -> FakeTerminal:
            return self

        def __exit__(self, *args: object) -> bool:
            return False

    monkeypatch.setattr("builtins.open", lambda *args, **kwargs: FakeTerminal(args[0]))
    monkeypatch.setattr(cli.os, "name", "nt")
    assert cli._read_terminal("Tenant URL") == "https://your-tenant.benchling.com\n"
    monkeypatch.setattr(cli.os, "name", "posix")

    def broken_open(*args, **kwargs):
        raise OSError("no tty")

    monkeypatch.setattr(cli.os, "open", broken_open)
    with pytest.raises(ValidationError, match="terminal is required"):
        cli._read_terminal("Tenant URL")

    monkeypatch.setattr(cli.sys, "stdin", type("In", (), {"read": lambda self: ""})())
    with pytest.raises(ValidationError, match="No client secret was received on stdin"):
        cli._read_secret(True)
    monkeypatch.setattr(cli.sys, "stdin", type("In", (), {"read": lambda self: "\n"})())
    with pytest.raises(ValidationError, match="No client secret was received on stdin"):
        cli._read_secret(True)
    monkeypatch.setattr("benchling_tools.cli.getpass.getpass", lambda prompt="": "")
    with pytest.raises(ValidationError, match="Client secret is required"):
        cli._read_secret(False)
    monkeypatch.setattr(
        "benchling_tools.cli.getpass.getpass",
        lambda prompt="": (_ for _ in ()).throw(EOFError()),
    )
    with pytest.raises(ValidationError, match="Client secret is required"):
        cli._read_secret(False)


def test_redact_removes_secrets_from_nested_values_and_credential_keys() -> None:
    register_secret("nope")
    register_secret(SECRET)
    cleaned = redact(
        {
            "note": f"prefix {SECRET} suffix",
            "access_token": TOKEN,
            "client_secret": "short",
            "items": (SECRET, {"refresh_token": "x"}),
            "count": 1,
        }
    )
    rendered = json.dumps(cleaned)
    assert SECRET not in rendered
    assert TOKEN not in rendered
    assert cleaned["access_token"] == "[redacted]"
    assert cleaned["count"] == 1


def test_build_environment_rejects_a_blank_client_id() -> None:
    with pytest.raises(ValidationError, match="Client ID is required"):
        build_environment("dev", "https://your-tenant.benchling.com", "  ")
    with pytest.raises(ValidationError, match="Invalid environment name"):
        build_environment("DEV", "https://your-tenant.benchling.com", "client")


@pytest.mark.asyncio
async def test_journal_and_success_result_cannot_contain_the_secret(fake, benchling_env) -> None:
    fake.stamp = SANDBOX_SECRET
    runtime = runtime_for(fake)
    try:
        planned = await runtime.planner.plan(
            ENTRY_ID, "document", {"operation": "delete", "from_part_id": TEXT_PART}
        )
        applied = await runtime.planner.apply(planned.id)
    finally:
        await runtime.close()
    rendered = json.dumps(applied)
    journal = (runtime.repository.config.journals_dir / f"{planned.id}.json").read_text()
    assert SANDBOX_SECRET not in rendered
    assert SANDBOX_SECRET not in journal
    assert "note" in journal


@pytest.mark.asyncio
async def test_failed_apply_envelope_and_journal_omit_the_secret(fake, benchling_env) -> None:
    fake.stamp = SANDBOX_SECRET
    fake.fail("PATCH", f"/entry/{ENTRY_ID}", 500)
    runtime = runtime_for(fake)
    try:
        planned = await runtime.planner.plan(
            ENTRY_ID, "document", {"operation": "delete", "from_part_id": TEXT_PART}
        )
        with pytest.raises(Exception) as caught:
            await runtime.planner.apply(planned.id)
    finally:
        await runtime.close()
    envelope = Envelope.failure(caught.value)
    rendered = envelope.model_dump_json()
    journal = (runtime.repository.config.journals_dir / f"{planned.id}.json").read_text()
    assert SANDBOX_SECRET not in rendered
    assert SANDBOX_SECRET not in journal


def test_environment_file_rejects_invalid_records() -> None:
    store = EnvironmentConfigStore()
    store.ensure()
    store.environments_path.write_text(
        json.dumps({"version": 1, "environments": {"DEV": {"tenant_url": "https://x"}}}),
        encoding="utf-8",
    )
    with pytest.raises(ValidationError, match="invalid environment name"):
        store.configured_names()
    store.environments_path.write_text(
        json.dumps({"version": 1, "environments": []}),
        encoding="utf-8",
    )
    with pytest.raises(ValidationError, match="invalid"):
        store.configured_names()
    store.environments_path.write_text(
        json.dumps({"version": 1, "environments": {"dev": "nope"}}),
        encoding="utf-8",
    )
    with pytest.raises(ValidationError, match="invalid"):
        store.resolve("dev")


def test_logout_restores_the_secret_when_the_file_update_fails(monkeypatch) -> None:
    configure_env("dev", "https://your-tenant.benchling.com/", secret=SECRET)

    def boom(self, name: str) -> bool:
        raise OSError("read only")

    monkeypatch.setattr(EnvironmentConfigStore, "remove", boom)
    with pytest.raises(OSError):
        logout(EnvironmentConfigStore(), Keychain(), "dev")
    assert Keychain().get("dev") == SECRET


def test_login_requires_a_secret_and_a_reachable_token_endpoint(monkeypatch) -> None:
    def raise_connect(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError(SECRET)

    with pytest.raises(ValidationError, match="Client secret is required"):
        login(
            EnvironmentConfigStore(),
            Keychain(),
            name="dev",
            tenant_url="https://your-tenant.benchling.com",
            client_id="dev-client",
            secret="   ",
            http=token_transport(FakeBenchling()),
        )
    with pytest.raises(Exception) as caught:
        login(
            EnvironmentConfigStore(),
            Keychain(),
            name="dev",
            tenant_url="https://your-tenant.benchling.com",
            client_id="dev-client",
            secret=SECRET,
            http=httpx.Client(transport=httpx.MockTransport(raise_connect)),
        )
    assert SECRET not in json.dumps(caught.value.details)
    assert not EnvironmentConfigStore().environments_path.exists()


def test_remaining_login_logout_and_prompt_edges(monkeypatch) -> None:
    import io

    store = EnvironmentConfigStore()
    monkeypatch.setattr(cli.sys, "stdin", io.StringIO("\n"))
    with pytest.raises(ValidationError, match="required"):
        cli._prompt("Tenant URL", tty=False)
    monkeypatch.setattr(cli.sys, "stdin", io.StringIO(SECRET + "\r\n"))
    assert cli._read_secret(True) == SECRET
    monkeypatch.setattr(cli.sys, "stdin", io.StringIO(SECRET + "\n"))
    assert cli._read_secret(True) == SECRET
    with pytest.raises(ValidationError, match="Invalid environment name"):
        cli._require_env_name("DEV", store)
    with pytest.raises(ValidationError, match="Invalid client ID"):
        build_environment("dev", "https://your-tenant.benchling.com", "bad\nid")

    bare = build_environment("dev", "https://your-tenant.benchling.com", "dev-client")
    bare.client_id = None
    with pytest.raises(ValidationError, match="Client ID is required"):
        store.upsert(bare)
    store.ensure()
    store.environments_path.write_text(
        json.dumps(
            {
                "version": 1,
                "environments": {
                    "dev": {"tenant_url": "https://your-tenant.benchling.com/", "client_id": "   "}
                },
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(ValidationError, match="Invalid client ID"):
        store.resolve("dev")
    assert store.remove("missing") is False

    def not_json(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="not-json")

    with pytest.raises(Exception, match="invalid token response"):
        login(
            store,
            Keychain(),
            name="dev",
            tenant_url="https://your-tenant.benchling.com",
            client_id="dev-client",
            secret=SECRET,
            http=httpx.Client(transport=httpx.MockTransport(not_json)),
        )


def test_save_rolls_back_a_new_environment_and_a_keychain_error(monkeypatch) -> None:
    fake = FakeBenchling()

    def write_then_fail(self, environment) -> None:
        self.ensure()
        self.environments_path.write_text("{}\n", encoding="utf-8")
        raise OSError("disk full")

    monkeypatch.setattr(EnvironmentConfigStore, "upsert", write_then_fail)
    with pytest.raises(AuthenticationError):
        login(
            EnvironmentConfigStore(),
            Keychain(),
            name="dev",
            tenant_url="https://your-tenant.benchling.com",
            client_id="dev-client",
            secret=SECRET,
            http=token_transport(fake),
        )
    assert Keychain().get("dev") is None
    assert not EnvironmentConfigStore().environments_path.exists()

    class Exploding(MemoryKeyring):
        def set_password(self, service: str, username: str, password: str) -> None:
            raise keyring.errors.KeyringError("locked")

    keyring.set_keyring(Exploding())
    with pytest.raises(AuthenticationError):
        login(
            EnvironmentConfigStore(),
            Keychain(),
            name="dev",
            tenant_url="https://your-tenant.benchling.com",
            client_id="dev-client",
            secret=SECRET,
            http=token_transport(fake),
        )

    def delete_fails(self, env: str) -> None:
        raise OSError("keychain")

    monkeypatch.setattr(Keychain, "delete", delete_fails)
    monkeypatch.setattr(EnvironmentConfigStore, "upsert", write_then_fail)
    keyring.set_keyring(MemoryKeyring())
    with pytest.raises(AuthenticationError, match="could not be restored"):
        login(
            EnvironmentConfigStore(),
            Keychain(),
            name="dev",
            tenant_url="https://your-tenant.benchling.com",
            client_id="dev-client",
            secret=SECRET,
            http=token_transport(fake),
        )


def test_logout_without_a_secret_still_removes_the_record(monkeypatch) -> None:
    configure_env("dev", "https://your-tenant.benchling.com/", secret=None)

    def boom(self, name: str) -> bool:
        raise OSError("read only")

    monkeypatch.setattr(EnvironmentConfigStore, "remove", boom)
    with pytest.raises(OSError):
        logout(EnvironmentConfigStore(), Keychain(), "dev")
    Keychain().delete("nobody")
    result = CliRunner().invoke(cli.app, ["auth", "logout", "--env", "missing"])
    assert result.exit_code == 2
    assert "not configured" in json.loads(result.stdout)["error"]["message"]
    store = EnvironmentConfigStore()
    store.environments_path.write_text("{", encoding="utf-8")
    listed = CliRunner().invoke(cli.app, ["auth", "status"])
    assert listed.exit_code == 2
    assert SECRET not in listed.stdout


def test_pool_loads_a_saved_environment_without_a_secret() -> None:
    from benchling_tools.runtime import RuntimePool, load_secrets

    configure_env("dev", "https://your-tenant.benchling.com/", secret=None)
    assert load_secrets("dev") == {}
    pool = RuntimePool()
    runtime = pool.get("dev")
    assert runtime.environment.name == "dev"
    assert runtime.secrets == {}


def test_login_command_requires_an_environment_name() -> None:
    result = CliRunner().invoke(cli.app, ["auth", "login"], input="x\n")
    assert result.exit_code == 2
    assert "required" in json.loads(result.stdout)["error"]["message"]


def test_status_check_reports_a_missing_secret(monkeypatch) -> None:
    configure_env("dev", "https://your-tenant.benchling.com/", secret=None)
    patch_token_client(monkeypatch, FakeBenchling())
    result = CliRunner().invoke(cli.app, ["auth", "status", "--check", "--env", "dev"])
    assert result.exit_code == 3
    row = json.loads(result.stdout)["error"]["details"]["environments"]["dev"]
    assert row["secret_present"] is False
    assert row["token_check"]["ok"] is False
    message = json.loads(result.stdout)["error"]["message"]
    assert (
        message == "Client secret is not in the keychain; run benchling-tools auth login --env dev"
    )
    assert fake_calls_were_not_made(result)


def fake_calls_were_not_made(result) -> bool:
    assert "oauth" not in result.stdout
    return True


def test_noninteractive_login_needs_no_tty_and_prints_no_getpass_warning(monkeypatch) -> None:
    import warnings

    fake = FakeBenchling()
    patch_token_client(monkeypatch, fake)
    monkeypatch.setattr(cli, "_prompts_available", lambda **kwargs: False)
    monkeypatch.setattr(
        "benchling_tools.cli.getpass.getpass",
        lambda prompt="": (_ for _ in ()).throw(AssertionError("getpass")),
    )
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        result = CliRunner().invoke(
            cli.app,
            [
                "auth",
                "login",
                "--env",
                "dev",
                "--tenant-url",
                "your-tenant.benchling.com/api/v2/",
                "--client-id",
                "dev-client",
                "--secret-stdin",
            ],
            input=SECRET + "\n",
        )
    assert result.exit_code == 0, result.stdout
    assert not any("echo" in str(item.message).lower() for item in caught)
    combined = result.stdout + result.stderr
    assert "GetPassWarning" not in combined
    assert "Password input may be echoed" not in combined
    assert json.loads(result.stdout)["data"]["tenant_url"] == "https://your-tenant.benchling.com"
    saved = json.loads(EnvironmentConfigStore().environments_path.read_text())
    assert saved["environments"]["dev"]["tenant_url"] == "https://your-tenant.benchling.com"


def test_login_without_a_tty_names_the_missing_flags(monkeypatch) -> None:
    monkeypatch.setattr(cli, "_prompts_available", lambda **kwargs: False)
    monkeypatch.setattr(
        "benchling_tools.cli.getpass.getpass",
        lambda prompt="": (_ for _ in ()).throw(AssertionError("getpass")),
    )
    result = CliRunner().invoke(cli.app, ["auth", "login", "--env", "dev"])
    assert result.exit_code == 2
    message = json.loads(result.stdout)["error"]["message"]
    assert message.count("Pass") == 1
    assert "--tenant-url" in message
    assert "--client-id" in message
    assert "--secret-stdin" in message
    assert "GetPassWarning" not in result.stderr
    assert "Password input may be echoed" not in result.stderr

    secret_only = CliRunner().invoke(
        cli.app,
        [
            "auth",
            "login",
            "--env",
            "dev",
            "--tenant-url",
            "https://your-tenant.benchling.com",
            "--client-id",
            "dev-client",
        ],
    )
    secret_message = json.loads(secret_only.stdout)["error"]["message"]
    assert secret_message.endswith("Pass --secret-stdin.")
    assert "--tenant-url" not in secret_message

    both = CliRunner().invoke(
        cli.app,
        [
            "auth",
            "login",
            "--env",
            "dev",
            "--client-id",
            "dev-client",
        ],
    )
    both_message = json.loads(both.stdout)["error"]["message"]
    assert "Pass --tenant-url and --secret-stdin." in both_message
    assert "--client-id" not in both_message


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://your-tenant.benchling.com/api/v3", "https://your-tenant.benchling.com"),
        ("your-tenant.benchling.com/", "https://your-tenant.benchling.com"),
        ("https://your-tenant.benchling.com/api/v2/", "https://your-tenant.benchling.com"),
    ],
)
def test_tenant_url_normalization(url: str, expected: str) -> None:
    environment = build_environment("dev", url, "dev-client")
    assert environment.base_url == expected


def test_http_tenant_url_is_rejected_separately_from_the_client_id() -> None:
    with pytest.raises(ValidationError, match=r"https://, not http://") as caught:
        build_environment("dev", "http://your-tenant.benchling.com/api/v2", "dev-client")
    assert "your-tenant.benchling.com" not in str(caught.value)
    assert caught.value.details["field"] == "tenant_url"
    with pytest.raises(ValidationError, match="Client ID is required") as client:
        build_environment("dev", "https://your-tenant.benchling.com", "  ")
    assert client.value.details["field"] == "client_id"


def test_invalid_environment_name_states_the_rule() -> None:
    result = CliRunner().invoke(cli.app, ["auth", "status", "--env", "Dev_1"])
    assert result.exit_code == 2
    message = json.loads(result.stdout)["error"]["message"]
    assert "Invalid environment name" in message
    assert "lowercase letters and digits" in message
    assert json.loads(result.stdout)["error"]["details"]["env"] is None


def test_tty_reader_accepts_a_terminal_python_refuses_to_open_for_update() -> None:
    import pty

    master, slave = pty.openpty()
    try:
        name = os.ttyname(slave)
        with pytest.raises(io.UnsupportedOperation, match="not seekable"):
            open(name, "r+", encoding="utf-8")
        os.write(master, b"stdin-client\n")
        assert cli._read_posix_tty(name, "Client ID: ").strip() == "stdin-client"
    finally:
        os.close(master)
        os.close(slave)


def test_keychain_diagnosis_names_dbus_and_a_locked_collection(monkeypatch) -> None:
    monkeypatch.delenv("DBUS_SESSION_BUS_ADDRESS", raising=False)
    monkeypatch.setattr("benchling_tools.keychain.provider_installed", lambda: True)
    monkeypatch.setattr("benchling_tools.keychain.secret_service_reachable", lambda: False)
    keyring.set_keyring(keyring.backends.fail.Keyring())
    with pytest.raises(AuthenticationError) as dbus_error:
        Keychain().require()
    assert dbus_error.value.details["cause"] == "dbus_unreachable"
    dbus_message = dbus_error.value.message
    assert dbus_message.count("\n") == 2
    assert "not reachable on D-Bus" in dbus_message
    assert "DBUS_SESSION_BUS_ADDRESS is unset." in dbus_message
    assert "is set, but" not in dbus_message
    assert "dbus-run-session" in dbus_message
    assert "auth login" in dbus_message.split("dbus-run-session", 1)[1]
    assert "gnome-keyring-daemon --unlock --components=secrets" in dbus_message
    assert "See the README section Headless Linux." in dbus_message
    assert "HOME, LOGNAME" not in dbus_message
    monkeypatch.setenv("DBUS_SESSION_BUS_ADDRESS", "unix:path=/tmp/bus")
    set_message = keychain_message("dbus_unreachable")
    assert (
        "DBUS_SESSION_BUS_ADDRESS is set, but this bus has no org.freedesktop.secrets name."
        in set_message
    )
    assert "is unset" not in set_message
    assert set_message.count("\n") == 2

    class Locked(MemoryKeyring):
        def get_password(self, service: str, username: str) -> str | None:
            raise keyring.errors.KeyringLocked("locked")

    keyring.set_keyring(Locked())
    with pytest.raises(AuthenticationError) as locked_error:
        Keychain().get("dev")
    assert locked_error.value.details["cause"] == "locked"
    locked_message = locked_error.value.message
    assert locked_message.count("\n") == 2
    assert "collection is locked" in locked_message
    assert "dbus-run-session" in locked_message
    assert "auth login" in locked_message
    assert "See the README section Headless Linux." in locked_message
    assert "HOME, LOGNAME" not in locked_message
    assert SECRET not in locked_message


def test_secret_service_probe_and_logging_flags(monkeypatch) -> None:
    from benchling_tools import keychain as keychain_module

    monkeypatch.delenv("DBUS_SESSION_BUS_ADDRESS", raising=False)
    assert keychain_module.secret_service_reachable() is False
    monkeypatch.setenv("DBUS_SESSION_BUS_ADDRESS", "unix:path=/tmp/benchling-no-such-bus")
    assert keychain_module.secret_service_reachable() is False

    class Conn:
        def __init__(self) -> None:
            self.closed = False

        def close(self) -> None:
            self.closed = True

    conn = Conn()
    owned = keychain_module._secret_service_owned
    monkeypatch.setenv("DBUS_SESSION_BUS_ADDRESS", "unix:path=/tmp/bus")
    monkeypatch.setattr(keychain_module, "_open_session_bus", lambda: conn)
    monkeypatch.setattr(keychain_module, "_secret_service_owned", lambda connection: True)
    assert keychain_module.secret_service_reachable() is True
    assert conn.closed is True

    class FakeProxy:
        def __init__(self, bus: object, connection: object) -> None:
            self.connection = connection

        def NameHasOwner(self, name: str) -> tuple[int]:
            assert name == "org.freedesktop.secrets"
            return (0,)

    monkeypatch.setattr(keychain_module, "_secret_service_owned", owned)
    import jeepney.io.blocking as blocking

    monkeypatch.setattr(blocking, "Proxy", FakeProxy)
    assert keychain_module._secret_service_owned(object()) is False

    def broken_open() -> object:
        raise OSError("bus")

    monkeypatch.setattr(keychain_module, "_open_session_bus", broken_open)
    assert keychain_module.secret_service_reachable() is False

    def broken_owned(connection: object) -> bool:
        raise OSError("owner")

    monkeypatch.setattr(keychain_module, "_open_session_bus", lambda: conn)
    monkeypatch.setattr(keychain_module, "_secret_service_owned", broken_owned)
    assert keychain_module.secret_service_reachable() is False

    keychain_module._close_bus(object())

    class BadClose:
        def close(self) -> None:
            raise OSError("close")

    keychain_module._close_bus(BadClose())

    monkeypatch.setattr(keychain_module, "provider_installed", lambda: True)
    monkeypatch.setattr(keychain_module, "secret_service_reachable", lambda: True)
    assert keychain_module.classify_keychain_failure(object(), RuntimeError("down")) == "locked"
    unknown = keychain_module._keychain_error(None, None)
    assert unknown.details == {"backend": "unknown", "cause": "no_backend"}


def test_prompt_helpers_and_url_error_wording(monkeypatch) -> None:
    from pydantic import ValidationError as ModelValidationError

    from benchling_tools.models import (
        Environment,
        _tenant_url_message,
        explain_environment_error,
        prepare_tenant_url,
    )
    from benchling_tools.services import _doctor_hint

    class BrokenStdin:
        def isatty(self) -> bool:
            raise OSError("stdin")

    monkeypatch.setattr(cli.sys, "stdin", BrokenStdin())
    assert cli._stdin_is_tty() is False

    monkeypatch.setattr(cli.os, "name", "nt")

    class Console:
        def __enter__(self) -> Console:
            return self

        def __exit__(self, *args: object) -> bool:
            return False

    monkeypatch.setattr("builtins.open", lambda *args, **kwargs: Console())
    assert cli._controlling_tty_available() is True
    monkeypatch.setattr(
        "builtins.open", lambda *args, **kwargs: (_ for _ in ()).throw(OSError("con"))
    )
    assert cli._controlling_tty_available() is False

    monkeypatch.setattr(cli.os, "name", "posix")
    monkeypatch.setattr(cli.os, "open", lambda *args, **kwargs: 7)
    monkeypatch.setattr(cli.os, "close", lambda fd: None)
    assert cli._controlling_tty_available() is True
    assert cli._prompts_available(secret_on_stdin=True) is True
    monkeypatch.setattr(cli, "_stdin_is_tty", lambda: False)
    monkeypatch.setattr(cli, "_controlling_tty_available", lambda: False)
    assert cli._prompts_available(secret_on_stdin=False) is False

    monkeypatch.setattr(cli.getpass, "getpass", lambda prompt="": "")
    with pytest.raises(ValidationError, match="Client secret is required"):
        cli._read_secret(False)

    assert prepare_tenant_url(None) is None
    with pytest.raises(ValueError, match="Tenant URL is required"):
        prepare_tenant_url("  ")
    with pytest.raises(ValueError, match="Tenant URL must use https://"):
        prepare_tenant_url("ftp://your-tenant.benchling.com")
    assert _tenant_url_message("must use https") == "Tenant URL must use https://."
    assert _tenant_url_message("field required") == "Tenant URL is required."
    assert "Expected https://host" in _tenant_url_message("unrecognized")

    with pytest.raises(ModelValidationError) as invalid_id:
        Environment.model_validate(
            {
                "name": "dev",
                "tenant_url": "https://your-tenant.benchling.com",
                "auth_method": "client_credentials",
                "client_id": ["not-a-string"],
            }
        )
    explained = explain_environment_error(invalid_id.value, env="dev")
    assert explained.details["field"] == "client_id"
    with pytest.raises(ModelValidationError) as invalid_name:
        Environment.model_validate(
            {
                "name": "",
                "tenant_url": "https://your-tenant.benchling.com",
                "auth_method": "client_credentials",
                "client_id": "dev-client",
            }
        )
    generic = explain_environment_error(invalid_name.value, env="dev")
    assert generic.details["field"] == "tenant_url"
    assert "your-tenant.benchling.com" not in generic.message

    class HttpUrl:
        scheme = "http"
        username = None
        password = None
        path = ""
        query = None
        fragment = None

    with pytest.raises(ValueError, match="HTTPS"):
        Environment.strip_url_path(HttpUrl())  # type: ignore[arg-type]

    assert _doctor_hint(RuntimeError("unexpected"), "dev").startswith(
        "Run benchling-tools doctor --env dev"
    )
    summary = cli._doctor_failure_message({"checks": [{"ok": True}]})
    assert summary == "Doctor checks failed"
    echoed = cli._doctor_failure_message(
        {"checks": [{"ok": False, "error": "Check the client ID and secret", "hint": "client ID"}]}
    )
    assert echoed == "Check the client ID and secret"
