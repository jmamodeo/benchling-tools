"""Protected environments: writes need the name on the command line and a confirmation."""

from __future__ import annotations

import json

import httpx
import pytest
from conftest import configure_env, runtime_for
from fake_benchling import ENTRY_ID, TABLE_PART, TEXT_PART, FakeBenchling
from mcp import Client
from typer.testing import CliRunner

from benchlingtk import cli
from benchlingtk.environment import EnvironmentConfigStore, check_apply_allowed
from benchlingtk.errors import ValidationError
from benchlingtk.mcp_server import build_mcp_server

SECRET = "sensitive-test-secret"
DELETE_SPEC = {"operation": "delete", "from_part_id": TEXT_PART}


@pytest.fixture
def invoke(benchling_env, fake, monkeypatch):
    monkeypatch.setattr(cli, "Runtime", lambda environment: runtime_for(fake, environment))

    def run(*args: str, env: str | None = "sandbox", input: str | None = None):
        prefix = ["--env", env] if env else []
        result = CliRunner().invoke(cli.app, [*prefix, *args], input=input)
        return result.exit_code, json.loads(result.stdout)

    return run


def _plan(invoke, tmp_path) -> str:
    spec = tmp_path / "delete.json"
    spec.write_text(json.dumps(DELETE_SPEC), encoding="utf-8")
    code, planned = invoke(
        "changes", "plan", "--entry", ENTRY_ID, "--family", "document", "--spec", str(spec)
    )
    assert code == 0, planned
    return str(planned["data"]["id"])


def test_protected_apply_needs_confirm_env_matching_env(invoke, fake, tmp_path) -> None:
    code, protected = invoke("auth", "protect")
    assert code == 0 and protected["data"] == {"env": "sandbox", "protected": True}
    plan_id = _plan(invoke, tmp_path)

    code, refused = invoke("changes", "apply", plan_id)
    assert code == 2
    assert refused["error"]["code"] == "validation_error"
    assert "--confirm-env sandbox" in refused["error"]["message"]
    assert refused["error"]["details"] == {"env": "sandbox", "protected": True}

    code, mismatched = invoke("changes", "apply", plan_id, "--confirm-env", "other")
    assert code == 2
    assert "does not match --env sandbox" in mismatched["error"]["message"]
    assert fake.writes() == []

    _, status = invoke("changes", "status", plan_id)
    assert status["data"]["journal"]["status"] == "pending"

    code, applied = invoke("changes", "apply", plan_id, "--confirm-env", "sandbox")
    assert code == 0, applied
    assert applied["data"]["journal"]["status"] == "succeeded"
    assert TEXT_PART not in fake.part_ids()


def test_reads_and_previews_on_a_protected_env_are_unaffected(invoke, fake, tmp_path) -> None:
    EnvironmentConfigStore().set_protected("sandbox", True)
    code, resolved = invoke("entry", "resolve", ENTRY_ID)
    assert code == 0 and resolved["data"]["entry_id"] == ENTRY_ID
    _plan(invoke, tmp_path)
    assert fake.writes() == []


def test_unprotected_apply_needs_no_confirmation_but_rejects_a_wrong_one(
    invoke, fake, tmp_path
) -> None:
    plan_id = _plan(invoke, tmp_path)
    code, mismatched = invoke("changes", "apply", plan_id, "--confirm-env", "prod")
    assert code == 2 and mismatched["error"]["details"]["protected"] is False
    assert fake.writes() == []
    code, applied = invoke("changes", "apply", plan_id)
    assert code == 0 and applied["data"]["journal"]["status"] == "succeeded"


def test_guard_requires_env_from_the_command_line() -> None:
    environment = configure_env("prod", "https://your-tenant.benchling.com")
    environment = environment.model_copy(update={"protected": True})
    with pytest.raises(ValidationError, match="on the command line"):
        check_apply_allowed(environment, env_on_command_line=False, confirm_env="prod")
    check_apply_allowed(environment, env_on_command_line=True, confirm_env="prod")


def test_root_env_option_records_that_it_came_from_the_command_line(monkeypatch) -> None:
    seen: list[cli.State] = []

    class Recorder(cli.State):
        def __init__(self, *args, **kwargs) -> None:
            super().__init__(*args, **kwargs)
            seen.append(self)

    monkeypatch.setattr(cli, "State", Recorder)
    monkeypatch.setattr(cli, "_execute", lambda *args: None)
    CliRunner().invoke(cli.app, ["--env", "dev", "changes", "apply", "a" * 32])
    CliRunner().invoke(cli.app, ["changes", "apply", "a" * 32])
    assert [(state.env, state.env_on_command_line) for state in seen] == [
        ("dev", True),
        (None, False),
    ]


def test_doctor_and_auth_status_show_protection(invoke) -> None:
    code, doctor = invoke("doctor")
    assert code == 0 and doctor["data"]["protected"] is False
    assert doctor["data"]["protection"].startswith("not protected")
    EnvironmentConfigStore().set_protected("sandbox", True)
    code, doctor = invoke("doctor")
    assert doctor["data"]["protected"] is True
    assert doctor["data"]["protection"] == (
        "protected: `changes apply` needs `--env sandbox --confirm-env sandbox`; "
        "MCP `changes_apply` needs `--allow-writes sandbox`"
    )
    result = CliRunner().invoke(cli.app, ["auth", "status"])
    assert json.loads(result.stdout)["data"]["environments"]["sandbox"]["protected"] is True
    code, cleared = invoke("auth", "unprotect")
    assert code == 0 and cleared["data"]["protected"] is False
    assert EnvironmentConfigStore().resolve("sandbox").protected is False


def test_protect_unknown_environment_is_a_validation_error(benchling_env) -> None:
    result = CliRunner().invoke(cli.app, ["auth", "protect", "--env", "prod"])
    assert result.exit_code == 2
    assert json.loads(result.stdout)["error"]["details"]["env"] == "prod"


def _login(monkeypatch, *flags: str) -> dict:
    fake = FakeBenchling()
    monkeypatch.setattr(
        "benchlingtk.credentials.open_token_client",
        lambda: httpx.Client(transport=httpx.MockTransport(fake.handler)),
    )
    result = CliRunner().invoke(
        cli.app,
        [
            "auth",
            "login",
            "--env",
            "prod",
            "--tenant-url",
            "https://your-tenant.benchling.com",
            "--client-id",
            "prod-client",
            "--secret-stdin",
            *flags,
        ],
        input=SECRET,
    )
    assert result.exit_code == 0, result.stdout
    assert SECRET not in result.stdout
    return json.loads(result.stdout)["data"]


def test_login_flag_sets_protection_and_relogin_keeps_it(monkeypatch) -> None:
    assert _login(monkeypatch)["protected"] is False
    assert _login(monkeypatch, "--protected")["protected"] is True
    saved = json.loads(EnvironmentConfigStore().environments_path.read_text())
    assert saved["environments"]["prod"]["protected"] is True
    assert _login(monkeypatch)["protected"] is True
    assert _login(monkeypatch, "--unprotected")["protected"] is False
    assert EnvironmentConfigStore().resolve("prod").protected is False


def test_damaged_protected_flag_fails_closed() -> None:
    store = EnvironmentConfigStore()
    store.ensure()
    store.environments_path.write_text(
        json.dumps(
            {
                "version": 1,
                "environments": {
                    "prod": {
                        "tenant_url": "https://your-tenant.benchling.com",
                        "client_id": "prod-client",
                        "protected": "yes",
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(ValidationError) as caught:
        store.resolve("prod")
    assert caught.value.details == {"env": "prod", "field": "protected"}
    assert store.saved_protection("prod") is True
    assert store.saved_protection("dev") is False


async def _mcp_call(server, tool: str, **arguments):
    async with Client(server) as client:
        result = await client.call_tool(tool, arguments)
    assert result.is_error is False, result
    return result.structured_content


async def test_mcp_apply_on_protected_env_needs_allow_writes(pool, fake) -> None:
    EnvironmentConfigStore().set_protected("sandbox", True)
    reader = build_mcp_server(pool)
    listed = await _mcp_call(reader, "auth_status")
    assert listed["data"]["environments"]["sandbox"]["protected"] is True
    planned = await _mcp_call(
        reader,
        "changes_plan",
        env="sandbox",
        entry=ENTRY_ID,
        family="table",
        spec={
            "operation": "append_rows",
            "table_part_id": TABLE_PART,
            "rows": [["D", "4"]],
        },
    )
    plan_id = planned["data"]["id"]
    refused = await _mcp_call(reader, "changes_apply", env="sandbox", plan_id=plan_id)
    assert refused["error"]["code"] == "unsupported_capability"
    assert "protected" in refused["error"]["message"]
    assert "--allow-writes sandbox" in refused["error"]["message"]
    assert refused["error"]["details"] == {"env": "sandbox", "protected": True}
    assert fake.writes() == []

    writer = build_mcp_server(pool, writable_envs={"sandbox"})
    applied = await _mcp_call(writer, "changes_apply", env="sandbox", plan_id=plan_id)
    assert applied["ok"] is True, applied


def test_protected_refusal_happens_before_keychain_or_network(benchling_env, monkeypatch) -> None:
    EnvironmentConfigStore().set_protected("sandbox", True)

    def no_runtime(env):
        raise AssertionError("Runtime must not be built for a refused apply")

    monkeypatch.setattr(cli, "Runtime", no_runtime)
    result = CliRunner().invoke(cli.app, ["--env", "sandbox", "changes", "apply", "a" * 32])
    assert result.exit_code == 2
    assert json.loads(result.stdout)["error"]["details"] == {"env": "sandbox", "protected": True}
