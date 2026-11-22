from __future__ import annotations

import json
import os
import ssl
import stat
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from urllib.parse import parse_qs

import httpx
import pytest
from conftest import configure_env
from mcp.shared.memory import create_connected_server_and_client_session as Client
from typer.testing import CliRunner

from benchlingtk.auth import AuthManager
from benchlingtk.cli import app
from benchlingtk.environment import ENV_NAME, EnvironmentConfigStore, write_private
from benchlingtk.errors import AuthenticationError, ValidationError
from benchlingtk.keychain import SERVICE_NAME, Keychain
from benchlingtk.mcp_server import build_mcp_server
from benchlingtk.models import AuthMethod, Environment
from benchlingtk.runtime import Runtime, RuntimePool
from benchlingtk.tls import verified_context

SECRET = "sensitive-test-secret"


def test_tls_verification_remains_enabled():
    context = verified_context()
    assert context.verify_mode == ssl.CERT_REQUIRED
    assert context.check_hostname is True


def test_environment_file_ignores_legacy_files_and_variables(monkeypatch):
    home = EnvironmentConfigStore()
    home.ensure()
    (home.root / "profiles.json").write_text(
        '{"version": 1, "profiles": {"dev": {"tenant_url": "https://old.benchling.com"}}}'
    )
    monkeypatch.setenv("BENCHLING_DEV_API_ENDPOINT", "https://ignored.benchling.com/")
    monkeypatch.setenv("BENCHLING_DEV_CLIENT_ID", "ignored-client")
    monkeypatch.setenv("BENCHLING_DEV_CLIENT_SECRET", SECRET)
    with pytest.raises(ValidationError) as missing:
        home.resolve("dev")
    assert missing.value.details["available_envs"] == []
    configure_env("dev", "https://your-tenant.benchling.com/", "dev-client", SECRET)
    with pytest.raises(ValidationError) as required:
        home.resolve(None)
    assert required.value.details["available_envs"] == ["dev"]
    environment = home.resolve("dev")
    assert environment.base_url == "https://your-tenant.benchling.com"
    assert environment.auth_method == AuthMethod.CLIENT_CREDENTIALS
    assert environment.client_id == "dev-client"
    raw = home.environments_path.read_text()
    assert SECRET not in raw
    assert "ignored-client" not in raw
    assert stat.S_IMODE(home.environments_path.stat().st_mode) == 0o600
    assert Keychain().get("dev") == SECRET
    with pytest.raises(ValidationError):
        home.resolve("sandbox")


def test_multiple_environments_keep_separate_secrets():
    configure_env("dev", "https://your-tenant.benchling.com/", "dev-client", SECRET)
    configure_env(
        "test", "https://other-tenant.benchling.com/", "test-client", "other-secret-value"
    )
    store = EnvironmentConfigStore()
    assert store.configured_names() == ["dev", "test"]
    assert store.resolve("test").base_url == "https://other-tenant.benchling.com"
    saved = json.dumps(store.metadata())
    assert SECRET not in saved
    assert "other-secret-value" not in saved
    assert Keychain().get("dev") == SECRET
    assert Keychain().get("test") == "other-secret-value"


@pytest.mark.parametrize("name", ["DEV", "../dev", ""])
def test_invalid_environment_names_rejected(name):
    configure_env("dev", "https://your-tenant.benchling.com/", secret=SECRET)
    with pytest.raises(ValidationError):
        EnvironmentConfigStore().resolve(name)


def test_missing_secret_still_resolves_the_saved_environment():
    configure_env("dev", "https://your-tenant.benchling.com/", secret=None)
    environment = EnvironmentConfigStore().resolve("dev")
    auth = AuthManager({})
    with pytest.raises(AuthenticationError):
        auth.headers(environment)
    assert auth.status(environment)["secret_present"] is False
    assert SECRET not in json.dumps(auth.status(environment))


@pytest.mark.parametrize(
    "url",
    [
        "http://your-tenant.benchling.com",
        "https://user:sensitive-test-secret@your-tenant.benchling.com",
        "https://your-tenant.benchling.com/path",
    ],
)
def test_invalid_saved_endpoint_is_redacted(url):
    store = EnvironmentConfigStore()
    store.ensure()
    write_private(
        store.environments_path,
        json.dumps(
            {
                "version": 1,
                "environments": {"dev": {"tenant_url": url, "client_id": "dev-client"}},
            }
        ),
    )
    with pytest.raises(ValidationError) as caught:
        store.resolve("dev")
    assert url not in str(caught.value)
    assert SECRET not in str(caught.value.details)


def test_private_write_cleans_up_when_replace_or_open_fails(tmp_path, monkeypatch):
    target = tmp_path / "nested" / "environments.json"

    def refuse_fdopen(fd, mode="r", encoding=None):
        raise OSError("fdopen failed")

    monkeypatch.setattr(os, "fdopen", refuse_fdopen)
    with pytest.raises(OSError, match="fdopen"):
        write_private(target, "{}\n")
    monkeypatch.undo()

    def refuse_replace(src, dst):
        raise OSError("replace failed")

    monkeypatch.setattr(os, "replace", refuse_replace)
    with pytest.raises(OSError, match="replace"):
        write_private(target, "{}\n")
    assert not target.exists()
    assert not list(target.parent.glob(".environments.json.tmp"))


def test_private_write_survives_a_permissive_umask_and_chmod_failure(tmp_path, monkeypatch):
    target = tmp_path / "environments.json"
    previous = os.umask(0)
    try:
        write_private(target, "{}\n")
    finally:
        os.umask(previous)
    assert stat.S_IMODE(target.stat().st_mode) == 0o600

    def refuse(*args, **kwargs):
        raise OSError("chmod denied")

    monkeypatch.setattr(os, "chmod", refuse)
    write_private(target, "{}\n")
    assert target.read_text() == "{}\n"


def test_corrupt_environment_file_does_not_echo_its_contents(tmp_path):
    store = EnvironmentConfigStore(tmp_path)
    store.ensure()
    store.environments_path.write_text('{"version": 1, "environments": ' + SECRET, encoding="utf-8")
    with pytest.raises(ValidationError) as caught:
        store.resolve("dev")
    assert SECRET not in str(caught.value.details)
    store.environments_path.write_text('{"version": 9, "environments": {}}', encoding="utf-8")
    with pytest.raises(ValidationError, match="unsupported version"):
        store.configured_names()


def test_app_tokens_cached_renewed_and_not_persisted(tmp_path):
    environment = Environment(
        name="dev",
        tenant_url="https://your-tenant.benchling.com/",
        auth_method=AuthMethod.CLIENT_CREDENTIALS,
        client_id="test-client",
    )
    count = 0

    def handler(request):
        nonlocal count
        count += 1
        assert str(request.url) == "https://your-tenant.benchling.com/oauth/token"
        assert parse_qs(request.content.decode()) == {
            "grant_type": ["client_credentials"],
            "client_id": ["test-client"],
            "client_secret": [SECRET],
        }
        return httpx.Response(
            200,
            json={
                "access_token": f"memory-only-access-token-{count}",
                "token_type": "bearer",
                "expires_in": 900,
            },
        )

    auth = AuthManager(
        {environment.name: SECRET}, http=httpx.Client(transport=httpx.MockTransport(handler))
    )
    with ThreadPoolExecutor(max_workers=2) as pool:
        assert (
            list(pool.map(lambda _: auth.headers(environment), range(2)))
            == [{"Authorization": "Bearer memory-only-access-token-1"}] * 2
        )
    assert count == 1
    bundle = auth._app_tokens[(environment.name, environment.base_url, environment.client_id)]
    bundle.expires_at = datetime.now(UTC) - timedelta(seconds=1)
    assert auth.headers(environment) == {"Authorization": "Bearer memory-only-access-token-2"}
    assert auth.status(environment)["access_token_cached"] is True
    assert not (tmp_path / "token").exists()
    assert ENV_NAME.match("dev")


def test_token_error_cannot_echo_secret():
    environment = Environment(
        name="dev",
        tenant_url="https://your-tenant.benchling.com/",
        auth_method=AuthMethod.CLIENT_CREDENTIALS,
        client_id="test-client",
    )
    auth = AuthManager(
        {environment.name: SECRET},
        http=httpx.Client(
            transport=httpx.MockTransport(
                lambda _: httpx.Response(401, json={"error": SECRET, "client_secret": SECRET})
            )
        ),
    )
    with pytest.raises(AuthenticationError) as caught:
        auth.headers(environment)
    assert caught.value.message == (
        "Benchling at https://your-tenant.benchling.com rejected the client ID or secret."
    )
    assert caught.value.details == {
        "env": "dev",
        "tenant_url": "https://your-tenant.benchling.com",
        "status": 401,
    }
    assert SECRET not in json.dumps(caught.value.details)


def test_cli_status_lists_the_environment_without_a_secret_or_network(monkeypatch, tmp_path):
    configure_env("dev", "https://your-tenant.benchling.com/", "dev-client", SECRET)
    result = CliRunner().invoke(app, ["auth", "status", "--env", "dev"])
    assert result.exit_code == 0, result.stdout
    payload = json.loads(result.stdout)
    data = payload["data"]["environments"]["dev"]
    assert data["identity"] == "app"
    assert data["credential_source"] == "keyring"
    assert data["configured"] is True
    assert data["secret_present"] is True
    assert data["client_id"] == "dev-client"
    assert "token_check" not in data
    assert SECRET not in result.stdout
    assert SECRET not in result.stderr
    listed = json.dumps(EnvironmentConfigStore().metadata())
    assert SECRET not in listed
    assert SERVICE_NAME == "benchlingtk"


@pytest.mark.asyncio
async def test_mcp_uses_saved_app_identity(monkeypatch):
    configure_env("dev", "https://your-tenant.benchling.com/", "dev-client", SECRET)
    auth_http = httpx.Client(
        transport=httpx.MockTransport(
            lambda _: httpx.Response(
                200, json={"access_token": "memory-only-access-token", "expires_in": 900}
            )
        )
    )

    def read_handler(request):
        assert request.headers["Authorization"] == "Bearer memory-only-access-token"
        key = "items" if request.url.path.startswith("/api/v3/") else "entries"
        return httpx.Response(200, json={key: [], "nextToken": ""})

    runtime = Runtime(
        "dev",
        auth_http=auth_http,
        async_http=httpx.AsyncClient(transport=httpx.MockTransport(read_handler)),
    )
    pool = RuntimePool(runtimes={"dev": runtime})
    try:
        async with Client(build_mcp_server(pool)._mcp_server) as client:
            result = await client.call_tool("doctor", {"env": "dev"})
            rendered = json.dumps(result.structuredContent)
            assert result.structuredContent["ok"] is True
            assert result.structuredContent["data"]["auth_method"] == "client_credentials"
            assert SECRET not in rendered
            assert "memory-only-access-token" not in rendered
    finally:
        await pool.close()
        auth_http.close()


@pytest.mark.asyncio
async def test_mcp_routes_each_call_to_its_env():
    configure_env("dev", "https://your-tenant.benchling.com/", "dev-client", SECRET)
    configure_env(
        "test", "https://other-tenant.benchling.com/", "test-client", "other-secret-value"
    )
    hosts: list[str] = []

    def token_handler(request):
        return httpx.Response(200, json={"access_token": request.url.host, "expires_in": 900})

    def read_handler(request):
        assert request.headers["Authorization"] == f"Bearer {request.url.host}"
        hosts.append(request.url.host)
        key = "items" if request.url.path.startswith("/api/v3/") else "entries"
        return httpx.Response(200, json={key: [], "nextToken": ""})

    auth_http = httpx.Client(transport=httpx.MockTransport(token_handler))
    async_http = httpx.AsyncClient(transport=httpx.MockTransport(read_handler))
    pool = RuntimePool(
        runtimes={
            env: Runtime(env, auth_http=auth_http, async_http=async_http) for env in ("dev", "test")
        }
    )
    try:
        async with Client(build_mcp_server(pool, writable_envs={"dev"})._mcp_server) as client:
            listed = await client.call_tool("auth_status", {})
            environments = listed.structuredContent["data"]["environments"]
            assert {
                name: {key: row[key] for key in ("tenant_url", "protected", "writes_allowed")}
                for name, row in environments.items()
            } == {
                "dev": {
                    "tenant_url": "https://your-tenant.benchling.com",
                    "protected": False,
                    "writes_allowed": True,
                },
                "test": {
                    "tenant_url": "https://other-tenant.benchling.com",
                    "protected": False,
                    "writes_allowed": False,
                },
            }
            assert SECRET not in json.dumps(environments)
            for env, host in (
                ("test", "other-tenant.benchling.com"),
                ("dev", "your-tenant.benchling.com"),
            ):
                hosts.clear()
                result = await client.call_tool("doctor", {"env": env})
                assert result.structuredContent["meta"]["env"] == env
                assert hosts and set(hosts) == {host}
            blocked = await client.call_tool("changes_apply", {"env": "test", "plan_id": "a" * 32})
            assert blocked.structuredContent["error"]["code"] == "unsupported_capability"
            unknown = await client.call_tool("doctor", {"env": "prod"})
            assert unknown.structuredContent["error"]["details"]["available_envs"] == [
                "dev",
                "test",
            ]
            rendered = json.dumps(listed.structuredContent)
            assert SECRET not in rendered and "other-secret-value" not in rendered
    finally:
        await pool.close()
        auth_http.close()
