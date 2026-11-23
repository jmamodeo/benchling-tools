"""--include-pending on results list and results diff: pending results from this home's plans."""

from __future__ import annotations

import json

import pytest
from conftest import runtime_for
from fake_benchling import ENTRY_ID
from mcp.shared.memory import create_connected_server_and_client_session as Client
from typer.testing import CliRunner

from benchling_tools import cli, results_diff
from benchling_tools.config import ConfigStore
from benchling_tools.mcp_server import build_mcp_server

TABLE = "strtbl_example"
SCHEMA = "assaysch_abc123"
RERUN = "etr_rerun1"
NOTE = {"name": "Note", "columnId": "col_n", "fieldDefinitionApiId": "assayfield_note"}
CT = {"name": "Ct", "columnId": "col_c", "fieldDefinitionApiId": "assayfield_ct"}


def text(value: str) -> dict:
    return {"inputValue": {"__typename": "TextValue", "value": value}}


def number(value: float) -> dict:
    return {"inputValue": {"__typename": "DecimalValue", "value": value}}


def record(note: str, ct: int) -> dict:
    return {"schemaId": SCHEMA, "fields": {"note": {"value": note}, "ct": {"value": ct}}}


async def upload(runtime, fake, records) -> list[str]:
    planned = await runtime.planner.plan(
        ENTRY_ID, "results", {"table_id": TABLE, "assay_results": records}
    )
    await runtime.planner.apply(planned.id)
    return fake.complete_upload()


def _tables(fake) -> None:
    """A first push with one accepted row and a rerun entry with two rows."""
    fake.add_rows_table(
        "docpart_first", "Assay results", [NOTE, CT], [[text("A1"), number(12)]], schema_id=SCHEMA
    )
    fake.add_entry(RERUN, "EXP-2", "Synthetic rerun")
    fake.add_rows_table(
        "docpart_rerun",
        "Assay results",
        [NOTE, CT],
        [[text("A1"), number(12)], [text("B1"), number(16)]],
        schema_id=SCHEMA,
        entry_id=RERUN,
    )


async def test_diff_compares_pending_results_as_rows_of_their_table(runtime, fake) -> None:
    _tables(fake)
    pending_ids = await upload(runtime, fake, [record("B1", 15), record("C1", 20)])
    writes = len(fake.writes())

    plain = await results_diff.diff_results(runtime.service, left=ENTRY_ID, right=RERUN, key="Note")
    assert "pending" not in plain
    assert "pending_left" not in plain["tables"][0]["counts"]
    assert plain["tables"][0]["counts"]["only_right"] == 1

    diff = await results_diff.diff_results(
        runtime.service,
        left=ENTRY_ID,
        right=RERUN,
        key="Note",
        pending=runtime.planner.entry_pending_results,
    )

    home = {"schema_window": None, "this_home": {"plans": 1, "count": 2}}
    assert diff["pending"] == {
        "left": {
            "plans": 1,
            "pending": 2,
            "attached": 2,
            "not_attached": 0,
            "unplaced_count": 0,
            "not_found": 0,
            "sources": home,
        },
        "right": {
            "plans": 0,
            "pending": 0,
            "attached": 0,
            "not_attached": 0,
            "unplaced_count": 0,
            "not_found": 0,
            "sources": {"schema_window": None, "this_home": {"plans": 0, "count": 0}},
        },
    }
    assert diff["pending_unplaced"] == {"left": [], "right": []}
    assert results_diff.PENDING_UNPLACED_WARNING not in diff["warnings"]
    assert results_diff.PENDING_WARNING in diff["warnings"]
    [table] = diff["tables"]
    assert table["counts"] == {
        "left": 3,
        "right": 2,
        "matched": 2,
        "only_left": 1,
        "only_right": 0,
        "changed": 1,
        "duplicate_keys": 0,
        "pending_left": 2,
        "pending_right": 0,
    }
    assert table["changed"] == [
        {
            "key": "B1",
            "field": "Ct",
            "left": 15,
            "right": 16,
            "left_pending": True,
            "left_source": "this_home",
        }
    ]
    [only] = table["only_left"]
    assert only["source"] == "this_home"
    assert only["key"] == "C1" and only["pending"] is True
    assert only["row_index"] == 2
    assert only["values"] == {"Note": "C1", "Ct": 20}
    assert only["result_id"] == "result_up2"
    assert pending_ids
    assert len(fake.writes()) == writes


async def test_diff_filters_pending_rows_by_the_run_value(runtime, fake) -> None:
    _tables(fake)
    await upload(runtime, fake, [record("B1", 15), record("C1", 20)])

    diff = await results_diff.diff_results(
        runtime.service,
        left=f"{ENTRY_ID}:B1",
        right=f"{RERUN}:B1",
        run_field="note",
        fields=["Ct"],
        pending=runtime.planner.entry_pending_results,
    )

    [table] = diff["tables"]
    assert table["counts"]["left"] == 1 and table["counts"]["pending_left"] == 1
    assert table["changed"] == [
        {
            "key": 0,
            "field": "Ct",
            "left": 15,
            "right": 16,
            "left_pending": True,
            "left_source": "this_home",
        }
    ]


async def test_diff_skips_accepted_and_unplaced_pending_results(runtime, fake) -> None:
    _tables(fake)
    accepted, pending = await upload(runtime, fake, [record("B1", 15), record("C1", 20)])
    fake.accept_results([accepted])

    diff = await results_diff.diff_results(
        runtime.service,
        left=ENTRY_ID,
        right=RERUN,
        key="Note",
        tables=["Other table"],
        pending=runtime.planner.entry_pending_results,
    )

    assert diff["tables"] == [] and diff["tables_not_found"] == ["Other table"]
    assert diff["pending"]["left"] == {
        "plans": 1,
        "pending": 1,
        "attached": 0,
        "not_attached": 1,
        "unplaced_count": 0,
        "not_found": 0,
        "sources": {"schema_window": None, "this_home": {"plans": 1, "count": 1}},
    }
    assert pending


@pytest.fixture
def invoke(benchling_env, fake, monkeypatch):
    monkeypatch.setattr(cli, "Runtime", lambda environment: runtime_for(fake, environment))

    def run(*args: str):
        result = CliRunner().invoke(cli.app, [*args, "--env", "sandbox"])
        return result.exit_code, json.loads(result.stdout)

    return run


def _cli_upload(invoke, fake, tmp_path, records) -> list[str]:
    spec = tmp_path / "results.json"
    spec.write_text(json.dumps({"table_id": TABLE, "assay_results": records}))
    code, planned = invoke(
        "changes", "plan", "--entry", ENTRY_ID, "--family", "results", "--spec", str(spec)
    )
    assert code == 0, planned
    code, applied = invoke("changes", "apply", planned["data"]["id"])
    assert code == 0, applied
    return fake.complete_upload()


def test_cli_results_list_include_pending(invoke, fake, tmp_path) -> None:
    first, second = _cli_upload(invoke, fake, tmp_path, [record("B1", 15), record("C1", 20)])
    writes = len(fake.writes())

    code, plain = invoke("results", "list", "--entry", ENTRY_ID)
    assert code == 0, plain
    assert "pending" not in plain["data"]

    code, reply = invoke("results", "list", "--entry", "EXP-1", "--include-pending")
    assert code == 0, reply
    pending = reply["data"]["pending"]
    assert pending["sources"] == {"this_home": {"plans": 1, "count": 2}, "schema_window": None}
    assert pending["entry_id"] == ENTRY_ID
    assert all(item["source"] == "this_home" for item in pending["items"])
    assert len(pending["plans"]) == 1
    assert pending["count"] == 2 and pending["truncated"] is False
    assert [item["v2_id"] for item in pending["items"]] == [first, second]
    assert all(
        item["state"] == "pending" and item["table_id"] == TABLE for item in pending["items"]
    )
    assert '"Assay results"' in pending["next_step"]
    assert reply["data"]["items"] == plain["data"]["items"]

    code, page = invoke(
        "results", "list", "--entry", ENTRY_ID, "--include-pending", "--page-size", "1"
    )
    assert code == 0, page
    assert page["data"]["pending"]["count"] == 2
    assert len(page["data"]["pending"]["items"]) == 1
    assert page["data"]["pending"]["truncated"] is True

    code, by_id = invoke(
        "results", "list", "--entry", ENTRY_ID, "--include-pending", "--id", "result_up2"
    )
    assert code == 0, by_id
    assert [item["v2_id"] for item in by_id["data"]["pending"]["items"]] == [second]

    code, other = invoke(
        "results", "list", "--entry", ENTRY_ID, "--include-pending", "--schema", "assaysch_other"
    )
    assert code == 0, other
    assert other["data"]["pending"]["count"] == 0
    assert other["data"]["pending"]["next_step"] is None

    code, refused = invoke("results", "list", "--include-pending")
    assert code == 2 and refused["error"]["code"] == "validation_error"
    assert "include_pending needs entry" in refused["error"]["message"]

    code, refused = invoke(
        "results", "list", "--entry", ENTRY_ID, "--pending-created-after", "2026-11-19T00:00:00Z"
    )
    assert code == 2 and refused["error"]["code"] == "validation_error"
    assert "pending_created_after applies only with include_pending" in refused["error"]["message"]
    assert len(fake.writes()) == writes


async def test_results_list_pending_without_plans_points_elsewhere(runtime, fake, tmp_path) -> None:
    runtime.repository.config = ConfigStore(tmp_path / "empty-home")
    pending = await runtime.planner.results_list_pending(ENTRY_ID)
    assert pending["plans"] == [] and pending["items"] == [] and pending["count"] == 0
    assert "results pending --plan BUNDLE.json" in pending["next_step"]


def test_cli_results_diff_include_pending_writes_pending_columns(invoke, fake, tmp_path) -> None:
    _tables(fake)
    _cli_upload(invoke, fake, tmp_path, [record("B1", 15), record("C1", 20)])
    path = tmp_path / "diff.csv"

    code, reply = invoke(
        "results",
        "diff",
        "--left",
        ENTRY_ID,
        "--right",
        RERUN,
        "--key",
        "Note",
        "--include-pending",
        "--output",
        str(path),
        "--format",
        "csv",
    )

    assert code == 0, reply
    assert reply["data"]["tables"][0]["counts"]["pending_left"] == 2
    lines = path.read_text().splitlines()
    assert lines[0].endswith(",left_pending,right_pending")
    assert lines[1].startswith("Assay results,changed,B1,") and lines[1].endswith(",true,false")
    assert lines[2].startswith("Assay results,only_left,C1,") and lines[2].endswith(",true,false")

    plain = tmp_path / "plain.csv"
    code, reply = invoke(
        "results",
        "diff",
        "--left",
        ENTRY_ID,
        "--right",
        RERUN,
        "--key",
        "Note",
        "-o",
        str(plain),
        "--format",
        "csv",
    )
    assert code == 0, reply
    assert plain.read_text().splitlines()[0].endswith(",left_barcode,right_barcode")


async def test_mcp_tools_take_include_pending(pool, runtime, fake) -> None:
    _tables(fake)
    await upload(runtime, fake, [record("B1", 15)])
    server = build_mcp_server(pool)
    async with Client(server._mcp_server) as client:
        tools = {tool.name: tool for tool in (await client.list_tools()).tools}
        for name in ("results_list", "results_diff"):
            assert "include_pending" in tools[name].inputSchema["properties"]
            assert "pending_created_after" in tools[name].inputSchema["properties"]
        listed = await client.call_tool(
            "results_list", {"env": "sandbox", "entry": ENTRY_ID, "include_pending": True}
        )
        diffed = await client.call_tool(
            "results_diff",
            {
                "env": "sandbox",
                "left": ENTRY_ID,
                "right": RERUN,
                "key": "Note",
                "include_pending": True,
            },
        )
    listed_data = json.loads(listed.content[0].text)["data"]
    assert listed_data["pending"]["count"] == 1
    diffed_data = json.loads(diffed.content[0].text)["data"]
    assert diffed_data["tables"][0]["counts"]["pending_left"] == 1


WINDOW = "2026-11-19T00:00:30Z"


def sdk_push(fake, result_id: str, note: str, ct: int, *, entry_id=None, created="00:01:00") -> str:
    """A result pushed by another client: no plan in this home."""
    v2_id = f"5d000000-0000-4000-8000-{len(fake.assay_results):012d}"
    fake.assay_results.append(
        {
            "id": v2_id,
            "v3Id": result_id,
            "entryId": entry_id,
            "archiveRecord": None,
            "createdAt": f"2026-11-19T{created}Z",
            "schema": {"id": SCHEMA},
            "fields": {"note": {"value": note}, "ct": {"value": ct}},
            "isReviewed": False,
        }
    )
    return v2_id


async def test_results_list_reads_the_schema_window_and_keeps_this_home(runtime, fake) -> None:
    _tables(fake)
    first, second = await upload(runtime, fake, [record("B1", 15), record("C1", 20)])
    sdk = sdk_push(fake, "result_sdk1", "D1", 30)
    sdk_push(fake, "result_sdk2", "E1", 31, entry_id=RERUN)
    sdk_push(fake, "result_old", "F1", 32, created="00:00:00")
    for item in fake.assay_results:
        if item["id"] == first:
            item["createdAt"] = "2026-11-19T00:00:59Z"

    pending = await runtime.planner.results_list_pending(ENTRY_ID, created_after=WINDOW)

    assert [(item["v2_id"], item["source"]) for item in pending["items"]] == [
        (first, "this_home"),
        (second, "this_home"),
        (sdk, "schema_window"),
    ]
    assert pending["items"][2]["table_id"] is None
    window = pending["sources"]["schema_window"]
    assert window["created_after"] == WINDOW and window["schema_ids"] == [SCHEMA]
    assert window["count"] == 1 and window["complete"] is True
    assert "ties a pending result to no entry or table" in window["note"]
    assert pending["sources"]["this_home"] == {"plans": 1, "count": 2}
    assert pending["count"] == 3

    other = await runtime.planner.results_list_pending(
        ENTRY_ID, schema_id="assaysch_other", created_after=WINDOW
    )
    assert other["items"] == [] and other["sources"]["schema_window"]["schema_ids"] == []


async def test_schema_window_alone_finds_sdk_pushes(runtime, fake, tmp_path) -> None:
    _tables(fake)
    runtime.repository.config = ConfigStore(tmp_path / "empty-home")
    sdk = sdk_push(fake, "result_sdk1", "D1", 30)

    pending = await runtime.planner.results_list_pending(ENTRY_ID, created_after=WINDOW)

    assert [item["v2_id"] for item in pending["items"]] == [sdk]
    assert pending["plans"] == [] and pending["next_step"] is not None


async def test_capacity_list_and_diff_share_the_schema_query(runtime, fake, monkeypatch) -> None:
    _tables(fake)
    calls: list[tuple[str, str]] = []
    original = type(runtime.planner).schema_pending_records

    async def spy(self, schema_id, created_after):
        calls.append((schema_id, created_after))
        return await original(self, schema_id, created_after)

    monkeypatch.setattr(type(runtime.planner), "schema_pending_records", spy)

    await runtime.planner.results_capacity(
        ENTRY_ID, include_schema_pending=True, created_after=WINDOW
    )
    assert calls == [(SCHEMA, WINDOW)]
    await runtime.planner.results_list_pending(ENTRY_ID, created_after=WINDOW)
    assert calls[1:] == [(SCHEMA, WINDOW)]
    await results_diff.diff_results(
        runtime.service,
        left=ENTRY_ID,
        right=RERUN,
        key="Note",
        pending=runtime.planner.entry_pending_results,
        pending_created_after=WINDOW,
    )
    assert calls[2:] == [(SCHEMA, WINDOW), (SCHEMA, WINDOW)]


async def test_diff_places_a_schema_window_record_in_the_one_table(runtime, fake) -> None:
    _tables(fake)
    sdk_push(fake, "result_sdk1", "C1", 20)

    diff = await results_diff.diff_results(
        runtime.service,
        left=ENTRY_ID,
        right=RERUN,
        key="Note",
        pending=runtime.planner.entry_pending_results,
        pending_created_after=WINDOW,
    )

    assert results_diff.SCHEMA_WINDOW_NOTE in diff["warnings"]
    assert results_diff.PENDING_UNPLACED_WARNING not in diff["warnings"]
    [table] = diff["tables"]
    # Benchling ties the record to no entry, so both sides with that schema get it.
    assert table["counts"]["pending_left"] == 1 and table["counts"]["pending_right"] == 1
    assert table["counts"]["matched"] == 2 and table["changed"] == []
    [only_right] = table["only_right"]
    assert only_right["key"] == "B1" and "pending" not in only_right
    assert diff["pending"]["left"]["attached"] == 1
    assert diff["pending"]["left"]["sources"]["schema_window"]["count"] == 1


async def test_diff_lists_a_shared_schema_record_as_unplaced(runtime, fake) -> None:
    _tables(fake)
    fake.add_rows_table(
        "docpart_second", "More results", [NOTE, CT], [[text("Z1"), number(1)]], schema_id=SCHEMA
    )
    sdk = sdk_push(fake, "result_sdk1", "C1", 20)

    diff = await results_diff.diff_results(
        runtime.service,
        left=ENTRY_ID,
        right=RERUN,
        key="Note",
        pending=runtime.planner.entry_pending_results,
        pending_created_after=WINDOW,
    )

    assert results_diff.PENDING_UNPLACED_WARNING in diff["warnings"]
    assert diff["pending_unplaced"]["left"] == [
        {
            "id": sdk,
            "v3_id": "result_sdk1",
            "schema_id": SCHEMA,
            "created_at": "2026-11-19T00:01:00Z",
            "tables": ["Assay results", "More results"],
        }
    ]
    left = diff["pending"]["left"]
    assert left["unplaced_count"] == 1 and left["attached"] == 0 and left["not_attached"] == 0
    [table] = [table for table in diff["tables"] if table["table"] == "Assay results"]
    assert table["counts"]["pending_left"] == 0 and table["counts"]["pending_right"] == 1
    assert diff["pending_unplaced"]["right"] == []
    [placed] = [row for row in table["only_right"] if row.get("pending")]
    assert placed["key"] == "C1" and placed["source"] == "schema_window"
    assert placed["result_id"] == "result_sdk1"


async def test_diff_window_needs_pending(runtime, fake) -> None:
    _tables(fake)
    with pytest.raises(results_diff.ValidationError, match="applies only with include_pending"):
        await results_diff.diff_results(
            runtime.service, left=ENTRY_ID, right=RERUN, pending_created_after=WINDOW
        )


def test_cli_results_diff_window_needs_include_pending(invoke, fake) -> None:
    _tables(fake)
    code, refused = invoke(
        "results",
        "diff",
        "--left",
        ENTRY_ID,
        "--right",
        RERUN,
        "--pending-created-after",
        WINDOW,
    )
    assert code == 2 and refused["error"]["code"] == "validation_error"

    sdk_push(fake, "result_sdk1", "C1", 20)
    code, reply = invoke(
        "results",
        "list",
        "--entry",
        ENTRY_ID,
        "--include-pending",
        "--pending-created-after",
        WINDOW,
    )
    assert code == 0, reply
    assert [item["source"] for item in reply["data"]["pending"]["items"]] == ["schema_window"]
