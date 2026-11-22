"""Run the real `benchling-tools` CLI and MCP server against the offline fake Benchling API.

No credentials or network are used: HTTP is served in-process by
``tests/fake_benchling.py`` through ``httpx.MockTransport``, the same fake the test
suite uses. Every printed response is the genuine output of the CLI or server.

    uv run python scripts/demo.py
"""

from __future__ import annotations

import asyncio
import contextlib
import io
import json
import os
import sys
import tempfile
from pathlib import Path

import httpx
import keyring
from mcp.shared.memory import create_connected_server_and_client_session as Client

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests"))

from fake_benchling import ENTRY_ID, TABLE_PART, TENANT, FakeBenchling  # noqa: E402
from memory_keyring import MemoryKeyring  # noqa: E402

from benchling_tools import cli  # noqa: E402
from benchling_tools.environment import EnvironmentConfigStore  # noqa: E402
from benchling_tools.keychain import Keychain  # noqa: E402
from benchling_tools.mcp_server import build_mcp_server  # noqa: E402
from benchling_tools.models import AuthMethod, Environment  # noqa: E402
from benchling_tools.runtime import Runtime, RuntimePool  # noqa: E402

fake = FakeBenchling()


def _token_client() -> httpx.Client:
    def token(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"access_token": "demo", "expires_in": 900})

    return httpx.Client(transport=httpx.MockTransport(token))


def _runtime(env: str | None = "demo") -> Runtime:
    return Runtime(
        env,
        auth_http=_token_client(),
        async_http=httpx.AsyncClient(transport=httpx.MockTransport(fake.handler)),
    )


def capture(*args: str) -> tuple[int, dict]:
    """Run a CLI invocation in-process and return its exit code and parsed envelope."""
    buffer = io.StringIO()
    with contextlib.redirect_stdout(buffer):
        code = int(cli.app(list(args), standalone_mode=False) or 0)
    return code, json.loads(buffer.getvalue())


def run(*args: str, keep: tuple[str, ...] = (), brief_journal: bool = False) -> dict:
    """Echo a CLI invocation and print its envelope.

    The envelope's ``schema_version``, ``meta`` and null fields are omitted, ``keep`` limits
    ``data`` to the named keys, and ``brief_journal`` reduces a journal to its status and
    event stages. Trimming only drops fields for readability; every value shown is real
    output.
    """
    code, envelope = capture(*args)
    print(f"$ benchling-tools {' '.join(args)}")
    if keep and envelope["data"]:
        envelope = {**envelope, "data": {k: envelope["data"][k] for k in keep}}
    if brief_journal:
        journal = envelope["data"]["journal"]
        events = [{"stage": e["stage"], "status": e["status"]} for e in journal["events"]]
        envelope = {
            **envelope,
            "data": {"journal": {"status": journal["status"], "events": events}},
        }
    shown = {"ok": envelope["ok"], "data": envelope["data"], "error": envelope["error"]}
    print(json.dumps({k: v for k, v in shown.items() if v is not None}, indent=2))
    print(f"[exit {code}]\n")
    return envelope


def main() -> None:
    """Run the demo, restoring the process environment, working directory and CLI runtime."""
    saved = (dict(os.environ), os.getcwd(), cli.Runtime)
    try:
        _demo()
    finally:
        os.environ.clear()
        os.environ.update(saved[0])
        os.chdir(saved[1])
        cli.Runtime = saved[2]


def _demo() -> None:
    home = tempfile.mkdtemp(prefix="benchling-demo-")
    os.environ["BENCHLING_TOOLS_HOME"] = home
    keyring.set_keyring(MemoryKeyring())
    EnvironmentConfigStore().upsert(
        Environment(
            name="demo",
            tenant_url=TENANT,
            auth_method=AuthMethod.CLIENT_CREDENTIALS,
            client_id="demo-client-id",
        )
    )
    Keychain().set("demo", "demo-client-secret")
    cli.Runtime = _runtime

    os.chdir(home)
    spec_a = Path("set-cell-b2.json")
    spec_a.write_text(
        json.dumps(
            {
                "operation": "set_cells",
                "table_part_id": TABLE_PART,
                "updates": [{"row": 0, "column": 1, "value": "1.25"}],
            }
        )
    )
    spec_b = Path("set-cell-d3.json")
    spec_b.write_text(
        json.dumps(
            {
                "operation": "set_cells",
                "table_part_id": TABLE_PART,
                "updates": [{"row": 2, "column": 0, "value": "D"}],
            }
        )
    )

    print("# 1. Read an entry (envelope trimmed for readability)\n")
    run("entry", "inspect", ENTRY_ID, "--env", "demo", keep=("version_id", "project_id"))

    print("# 2. Preview: reads Benchling, writes nothing remotely\n")
    plan = ("changes", "plan", "--entry", ENTRY_ID, "--family", "table")
    first = run(
        *plan,
        "--spec",
        spec_a.name,
        "--env",
        "demo",
        keep=("id", "family", "env", "preconditions", "guarantees", "warnings"),
    )
    second = capture(*plan, "--spec", spec_b.name, "--env", "demo")[1]
    print(f"remote writes so far: {len(fake.writes())}\n")

    print("# 3. Apply the first plan\n")
    run("changes", "apply", first["data"]["id"], "--env", "demo", brief_journal=True)
    print("# 4. The second plan was previewed against the old entry version: apply is refused\n")
    run("changes", "apply", second["data"]["id"], "--env", "demo")
    print("# 5. A plan cannot be applied twice\n")
    run("changes", "apply", first["data"]["id"], "--env", "demo")
    print(f"remote writes in total: {len(fake.writes())}\n")

    print("# 6. MCP server: read-only unless --allow-writes ENV is passed\n")

    async def mcp_demo() -> None:
        pool = RuntimePool(runtimes={"demo": _runtime()})
        try:
            async with Client(build_mcp_server(pool)._mcp_server) as client:
                result = await client.call_tool(
                    "changes_apply", {"env": "demo", "plan_id": second["data"]["id"]}
                )
                print('changes_apply(env="demo", plan_id=...) ->')
                print(json.dumps(result.structuredContent["error"], indent=2))
        finally:
            await pool.close()

    asyncio.run(mcp_demo())


if __name__ == "__main__":
    main()
