"""An MCP tool replies with exactly the envelope its CLI command prints."""

from __future__ import annotations

import asyncio
import json

import pytest
from conftest import runtime_for
from fake_benchling import ENTRY_ID, TABLE_PART, TENANT
from mcp import Client
from typer.testing import CliRunner

from benchlingtk import cli
from benchlingtk.mcp_server import build_mcp_server

TABLE_SPEC = {"operation": "append_rows", "table_part_id": TABLE_PART, "rows": [["D", "4"]]}


@pytest.fixture
def cli_reply(benchling_env, fake, monkeypatch):
    monkeypatch.setattr(cli, "Runtime", lambda environment: runtime_for(fake, environment))

    def invoke(args: tuple[str, ...]) -> dict:
        result = CliRunner().invoke(cli.app, ["--env", "sandbox", *args])
        return json.loads(result.stdout)

    async def run(*args: str) -> dict:
        # The CLI starts its own event loop, so it runs outside the test's loop.
        return await asyncio.to_thread(invoke, args)

    return run


@pytest.fixture
def mcp_reply(pool):
    server = build_mcp_server(pool)

    async def run(tool: str, **arguments) -> dict:
        async with Client(server) as client:
            result = await client.call_tool(tool, {"env": "sandbox", **arguments})
        assert result.is_error is False, result
        return result.structured_content

    return run


def _without_plan_identity(envelope: dict) -> dict:
    """A new plan has a fresh id, timestamp and checksum; everything else must match."""
    data = dict(envelope["data"])
    for key in ("id", "created_at", "checksum"):
        data.pop(key, None)
    return {**envelope, "data": data}


def _without_clock(value):
    """Snapshot timing is read from the clock on each call, so it is left out."""
    if isinstance(value, dict):
        return {
            key: _without_clock(item)
            for key, item in value.items()
            if key not in ("started_at", "finished_at")
        }
    if isinstance(value, list):
        return [_without_clock(item) for item in value]
    return value


@pytest.mark.parametrize(
    ("args", "tool", "arguments"),
    [
        (["entry", "resolve", ENTRY_ID], "entry_resolve", {"reference": ENTRY_ID}),
        (["entry", "resolve", "EXP-99"], "entry_resolve", {"reference": "EXP-99"}),
        (
            [
                "entry",
                "get",
                ENTRY_ID,
                "--full",
                "--part-offset",
                "1",
                "--part-limit",
                "3",
                "--result-limit",
                "2",
                "--table-row-limit",
                "1",
            ],
            "entry_get",
            {
                "reference": ENTRY_ID,
                "full": True,
                "part_offset": 1,
                "part_limit": 3,
                "result_limit": 2,
                "table_row_limit": 1,
            },
        ),
        (
            ["results", "rows", "docpart_rt1", "--limit", "2"],
            "results_rows",
            {"part_id": "docpart_rt1", "limit": 2},
        ),
    ],
)
async def test_read_replies_match(cli_reply, mcp_reply, fake, args, tool, arguments) -> None:
    fake.add_results_table()
    printed = await cli_reply(*args)
    assert printed["meta"] == {"env": "sandbox", "tenant_url": TENANT}
    assert _without_clock(printed) == _without_clock(await mcp_reply(tool, **arguments))
    assert fake.writes() == []


async def test_changes_plan_and_status_replies_match(cli_reply, mcp_reply, fake) -> None:
    printed = await cli_reply(
        "changes",
        "plan",
        "--entry",
        ENTRY_ID,
        "--family",
        "table",
        "--spec-json",
        json.dumps(TABLE_SPEC),
    )
    assert printed["ok"] is True, printed
    replied = await mcp_reply("changes_plan", entry=ENTRY_ID, family="table", spec=TABLE_SPEC)
    assert _without_plan_identity(printed) == _without_plan_identity(replied)

    plan_id = printed["data"]["id"]
    assert await cli_reply("changes", "status", plan_id) == await mcp_reply(
        "changes_status", plan_id=plan_id
    )
    assert fake.writes() == []


async def test_export_replies_match(cli_reply, mcp_reply, fake, tmp_path) -> None:
    output = tmp_path / "table.csv"
    printed = await cli_reply(
        "export",
        "--kind",
        "table",
        "--part",
        TABLE_PART,
        "--format",
        "csv",
        "--output",
        str(output),
        "--overwrite",
    )
    assert printed["ok"] is True, printed
    replied = await mcp_reply(
        "export", kind="table", part_id=TABLE_PART, format="csv", output=str(output), overwrite=True
    )
    assert printed == replied
    assert fake.writes() == []
