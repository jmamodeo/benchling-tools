from __future__ import annotations

import os
import re

import httpx
import keyring
import pytest
import pytest_asyncio
from fake_benchling import TENANT, FakeBenchling
from memory_keyring import MemoryKeyring

from benchling_tools.api import BenchlingAPI
from benchling_tools.auth import AuthManager
from benchling_tools.environment import EnvironmentConfigStore
from benchling_tools.keychain import Keychain
from benchling_tools.models import AuthMethod, Environment
from benchling_tools.redact import clear_registered_secrets
from benchling_tools.runtime import Runtime, RuntimePool

SANDBOX_SECRET = "sandbox-client-secret"
_ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
# Rich on GitHub Actions forces a terminal and can insert styling inside option
# names such as --secret-stdin. These overrides plus ANSI stripping keep help
# assertions independent of that rendering.
_PLAIN_TERMINAL = {"NO_COLOR": "1", "TERM": "dumb", "COLUMNS": "200"}


def plain_stdout(args: list[str]) -> str:
    """Run the CLI and return stdout with ANSI escapes removed."""
    from typer.testing import CliRunner

    from benchling_tools import cli

    result = CliRunner().invoke(cli.app, args, env=_PLAIN_TERMINAL)
    return _ANSI.sub("", result.stdout)


@pytest.fixture(autouse=True)
def isolated_home(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("BENCHLING_TOOLS_HOME", str(tmp_path / "app"))


@pytest.fixture(autouse=True)
def memory_keyring() -> MemoryKeyring:
    """Install a fresh in-memory keychain and drop secrets tracked for redaction."""
    clear_registered_secrets()
    backend = MemoryKeyring()
    keyring.set_keyring(backend)
    yield backend
    clear_registered_secrets()
    keyring.set_keyring(keyring.backends.fail.Keyring())


def configure_env(
    name: str,
    tenant_url: str,
    client_id: str = "client",
    secret: str | None = SANDBOX_SECRET,
) -> Environment:
    """Save a non-secret environment record and, when given, its keychain secret."""
    environment = Environment(
        name=name,
        tenant_url=tenant_url,
        auth_method=AuthMethod.CLIENT_CREDENTIALS,
        client_id=client_id,
    )
    EnvironmentConfigStore().upsert(environment)
    if secret is not None:
        Keychain().set(name, secret)
    return environment


def make_environment(name: str = "sandbox") -> Environment:
    return Environment(
        name=name,
        tenant_url="https://tenant.benchling.com",
        auth_method=AuthMethod.CLIENT_CREDENTIALS,
        client_id="client",
    )


def make_api(handler, *, environment: Environment | None = None) -> BenchlingAPI:
    environment = environment or make_environment()

    def token(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, json={"access_token": "token", "token_type": "Bearer", "expires_in": 900}
        )

    auth = AuthManager(
        {environment.name: "secret"}, http=httpx.Client(transport=httpx.MockTransport(token))
    )
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return BenchlingAPI(environment, auth, http=client, max_read_attempts=1)


def token_client() -> httpx.Client:
    def token(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, json={"access_token": "token", "token_type": "Bearer", "expires_in": 900}
        )

    return httpx.Client(transport=httpx.MockTransport(token))


@pytest.fixture
def benchling_env(monkeypatch) -> None:
    for name in list(os.environ):
        if name.startswith("BENCHLING_") and name != "BENCHLING_TOOLS_HOME":
            monkeypatch.delenv(name)
    configure_env("sandbox", TENANT, "client", SANDBOX_SECRET)


@pytest.fixture
def fake() -> FakeBenchling:
    return FakeBenchling()


def runtime_for(fake: FakeBenchling, env: str = "sandbox") -> Runtime:
    return Runtime(
        env,
        auth_http=token_client(),
        async_http=httpx.AsyncClient(transport=httpx.MockTransport(fake.handler)),
        download_transport=httpx.MockTransport(fake.handler),
    )


@pytest_asyncio.fixture
async def runtime(benchling_env, fake):
    active = runtime_for(fake)
    yield active
    await active.close()


@pytest_asyncio.fixture
async def pool(benchling_env, fake):
    runtimes = RuntimePool(runtimes={"sandbox": runtime_for(fake)})
    yield runtimes
    await runtimes.close()
