"""An MCP tool replies with exactly the envelope its CLI command prints."""

from __future__ import annotations

import asyncio
import json

import pytest
from conftest import runtime_for
from fake_benchling import ENTRY_ID, TABLE_PART, TENANT
from mcp.shared.memory import create_connected_server_and_client_session as Client
from typer.testing import CliRunner

from benchling_tools import cli
from benchling_tools.environment import EnvironmentConfigStore
from benchling_tools.mcp_server import build_mcp_server

RESULT_SPEC = {
    "table_id": "strtbl_example",
    "assay_results": [{"schemaId": "assaysch_abc123", "fields": {"ct": {"value": 12}}}],
}
TABLE_SPEC = {"operation": "append_rows", "table_part_id": TABLE_PART, "rows": [["D", "4"]]}


@pytest.fixture
def cli_reply(benchling_env, fake, monkeypatch):
    monkeypatch.setattr(cli, "Runtime", lambda environment: runtime_for(fake, environment))

    def invoke(args: tuple[str, ...]) -> dict:
        result = CliRunner().invoke(cli.app, [*args, "--env", "sandbox"])
        return json.loads(result.stdout)

    async def run(*args: str) -> dict:
        # The CLI starts its own event loop, so it runs outside the test's loop.
        return await asyncio.to_thread(invoke, args)

    return run


@pytest.fixture
def mcp_reply(pool):
    server = build_mcp_server(pool)

    async def run(tool: str, **arguments) -> dict:
        async with Client(server._mcp_server) as client:
            result = await client.call_tool(tool, {"env": "sandbox", **arguments})
        assert result.isError is False, result
        return result.structuredContent

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
        (
            ["users", "list", "--handle", "fixture_handle_1"],
            "users_list",
            {"handle": ["fixture_handle_1"]},
        ),
        (
            ["users", "get", "fixture_handle_1"],
            "users_get",
            {"user_id_or_handle": "fixture_handle_1"},
        ),
        (
            ["entry", "list", "--creator", "fixture_handle_1"],
            "entry_list",
            {"creator": ["fixture_handle_1"]},
        ),
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
            ["results", "pending", "--entry", "EXP-1", "--limit", "2"],
            "results_pending",
            {"entry": "EXP-1", "limit": 2},
        ),
        (
            ["results", "capacity", ENTRY_ID],
            "results_capacity",
            {"reference": ENTRY_ID},
        ),
        (
            [
                "results",
                "capacity",
                ENTRY_ID,
                "--spec-json",
                json.dumps(RESULT_SPEC),
                "--include-schema-pending",
                "--created-after",
                "2026-01-01T00:00:00Z",
            ],
            "results_capacity",
            {
                "reference": ENTRY_ID,
                "spec": RESULT_SPEC,
                "include_schema_pending": True,
                "created_after": "2026-01-01T00:00:00Z",
            },
        ),
        (
            ["results", "rows", "docpart_rt1", "--limit", "2"],
            "results_rows",
            {"part_id": "docpart_rt1", "limit": 2},
        ),
        (
            ["container", "find", "--barcode", "BAR-SOURCE"],
            "container_find",
            {"barcodes": ["BAR-SOURCE"]},
        ),
        (
            ["container", "find", "--barcode", "BAR-SOURCE", "--barcode", "BAR-NONE"],
            "container_find",
            {"barcodes": ["BAR-SOURCE", "BAR-NONE"]},
        ),
    ],
)
async def test_read_replies_match(cli_reply, mcp_reply, fake, args, tool, arguments) -> None:
    fake.add_results_table()
    fake.add_example_results_part()
    printed = await cli_reply(*args)
    assert printed["meta"] == {"env": "sandbox", "tenant_url": TENANT}
    assert _without_clock(printed) == _without_clock(await mcp_reply(tool, **arguments))
    assert fake.writes() == []


@pytest.mark.parametrize(
    ("args", "tool", "arguments"),
    [
        (["results", "tables", ENTRY_ID], "results_tables", {"reference": ENTRY_ID}),
        (
            ["results", "tables", ENTRY_ID, "--no-counts"],
            "results_tables",
            {"reference": ENTRY_ID, "counts": False},
        ),
        (
            [
                "results",
                "tables",
                ENTRY_ID,
                "--name-prefix",
                "Panel Testing",
                "--where",
                "Processing run=run_a",
                "--where",
                "sample=con_tube1",
            ],
            "results_tables",
            {
                "reference": ENTRY_ID,
                "name_prefix": "Panel Testing",
                "where": ["Processing run=run_a", "sample=con_tube1"],
            },
        ),
        (
            [
                "results",
                "rows",
                "strtbl_panel2",
                "--entry",
                ENTRY_ID,
                "--where",
                "Sample=con_tube3",
            ],
            "results_rows",
            {"part_id": "strtbl_panel2", "entry": ENTRY_ID, "where": ["Sample=con_tube3"]},
        ),
        (
            ["results", "rows", "docpart_panel1", "--where", "Operator=me"],
            "results_rows",
            {"part_id": "docpart_panel1", "where": ["Operator=me"]},
        ),
    ],
)
async def test_results_count_and_filter_replies_match(
    cli_reply, mcp_reply, fake, args, tool, arguments
) -> None:
    fake.add_panel_tables()
    printed = await cli_reply(*args)
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


async def test_changes_status_task_replies_match(cli_reply, mcp_reply, fake) -> None:
    fake.tasks["task_parity1"] = {
        "status": "SUCCEEDED",
        "response": {"assayResults": [{"id": "00000000-0000-4000-8000-000000000001"}]},
    }
    printed = await cli_reply("changes", "status", "--task", "task_parity1")
    assert printed["ok"] is True, printed
    assert printed == await mcp_reply("changes_status", task_id="task_parity1")
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


async def test_unknown_entry_fails_with_the_same_envelope(cli_reply, mcp_reply, fake) -> None:
    printed = await cli_reply("entry", "get", "EXP-99", "--full")
    assert printed["ok"] is False, printed
    assert printed == await mcp_reply("entry_get", reference="EXP-99", full=True)
    assert fake.writes() == []


async def test_protected_apply_refusal_has_the_same_code(cli_reply, pool, fake) -> None:
    EnvironmentConfigStore().set_protected("sandbox", True)
    printed_plan = await cli_reply(
        "changes",
        "plan",
        "--entry",
        ENTRY_ID,
        "--family",
        "table",
        "--spec-json",
        json.dumps(TABLE_SPEC),
    )
    plan_id = printed_plan["data"]["id"]
    printed = await cli_reply("changes", "apply", plan_id)
    async with Client(build_mcp_server(pool, writable_envs={"sandbox"})._mcp_server) as client:
        result = await client.call_tool("changes_apply", {"env": "sandbox", "plan_id": plan_id})
    replied = result.structuredContent
    assert printed["ok"] is replied["ok"] is False
    assert printed["error"]["code"] == replied["error"]["code"] == "validation_error"
    assert printed["error"]["details"] == replied["error"]["details"]
    assert fake.writes() == []
