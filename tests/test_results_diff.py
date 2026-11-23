"""results diff: compare results-table rows of two entries, or two runs, read-only."""

from __future__ import annotations

import hashlib
import json
import tempfile

import pytest
from conftest import runtime_for
from fake_benchling import ENTRY_ID
from typer.testing import CliRunner

from benchling_tools import cli, results_diff
from benchling_tools.errors import ValidationError

RERUN = "etr_rerun1"
SCHEMA = "assaysch_diff"
WELL = {"name": "Well", "columnId": "col_w", "fieldDefinitionApiId": "assayfield_well"}
TUBE = {"name": "Tube", "columnId": "col_t", "fieldDefinitionApiId": "assayfield_tube"}
RUN = {"name": "Run", "columnId": "col_r", "fieldDefinitionApiId": "assayfield_run"}
CT = {"name": "Ct", "columnId": "col_c", "fieldDefinitionApiId": "assayfield_ct"}
REPORT = {"name": "Report", "columnId": "col_p", "fieldDefinitionApiId": "assayfield_report"}
BLOB_A = "00000000-0000-4000-8000-00000000000a"
BLOB_B = "00000000-0000-4000-8000-00000000000b"
BLOB_C = "00000000-0000-4000-8000-00000000000c"


def text(value: str) -> dict:
    return {"inputValue": {"__typename": "TextValue", "value": value}}


def number(value: float) -> dict:
    return {"inputValue": {"__typename": "DecimalValue", "value": value}}


def tube(container_id: str) -> dict:
    return {
        "inputValue": {
            "__typename": "ObjectLinkValue",
            "value": {"__typename": "Container", "id": container_id},
        }
    }


def link(value: object) -> dict:
    return {"inputValue": {"__typename": "ObjectLinkValue", "value": value}}


def _schema(fake) -> None:
    fake.field_defs[SCHEMA] = [
        {"id": "assayfield_well", "systemName": "well", "name": "Well"},
        {"id": "assayfield_tube", "systemName": "tube", "name": "Tube"},
        {"id": "assayfield_run", "systemName": "run_type", "name": "Run"},
        {"id": "assayfield_ct", "systemName": "ct", "name": "Ct"},
        {
            "id": "assayfield_report",
            "systemName": "report",
            "name": "Report",
            "__typename": "AttachmentLinkFieldDefinition",
        },
    ]


def _tubes(fake, count: int) -> None:
    for index in range(count):
        fake.containers.append(
            {"id": f"con_t{index}", "name": f"Tube {index}", "barcode": f"BC-{index:03d}"}
        )


def _first_and_rerun(fake) -> None:
    """A first push and a rerun of the same plate; B1's tube was reassigned."""
    _schema(fake)
    _tubes(fake, 3)
    fake.add_entry(RERUN, "EXP-2", "Synthetic rerun")
    columns = [WELL, TUBE, CT]
    fake.add_rows_table(
        "docpart_first",
        "qPCR Testing 1",
        columns,
        [
            [text("A1"), tube("con_t0"), number(12)],
            [text("B1"), tube("con_t1"), number(15)],
            [text("C1"), tube("con_t2"), number(20)],
        ],
    )
    fake.add_rows_table(
        "docpart_rerun",
        "qPCR Testing 1",
        columns,
        [
            [text("A1"), tube("con_t0"), number(12.0)],
            [text("B1"), tube("con_t2"), number(15)],
            [text("D1"), tube("con_t1"), number(30)],
        ],
        entry_id=RERUN,
    )


def _bulk_calls(fake) -> list:
    return [call for call in fake.calls if call.path.endswith("/containers:bulk-get")]


async def test_tables_pair_by_name_and_rows_by_a_text_key(runtime, fake) -> None:
    _first_and_rerun(fake)
    fake.add_rows_table("docpart_left_only", "Melt curve 1", [WELL], [[text("A1")]])
    fake.add_rows_table(
        "docpart_right_only", "Melt curve 2", [WELL], [[text("A1")]], entry_id=RERUN
    )
    fake.add_rows_table("docpart_s1", "Dilution", [WELL], [[text("A1")]])
    fake.add_rows_table(
        "docpart_s2", "Dilution", [WELL], [[text("A1")]], schema_id="assaysch_other", entry_id=RERUN
    )
    diff = await results_diff.diff_results(runtime.service, left=ENTRY_ID, right=RERUN, key="Well")
    assert diff["left"]["entry_id"] == ENTRY_ID and diff["right"]["display_id"] == "EXP-2"
    assert diff["pairing"] == "key" and diff["key"] == ["Well"]
    assert diff["tables_only_left"] == ["Melt curve 1"]
    assert diff["tables_only_right"] == ["Melt curve 2"]
    assert diff["schema_mismatch"] == [
        {
            "table": "Dilution",
            "left_part_id": "docpart_s1",
            "right_part_id": "docpart_s2",
            "left_schema_id": SCHEMA,
            "right_schema_id": "assaysch_other",
        }
    ]
    [table] = diff["tables"]
    assert table["table"] == "qPCR Testing 1"
    assert table["fields"] == ["Well", "Tube", "Ct"]
    assert table["counts"] == {
        "left": 3,
        "right": 3,
        "matched": 2,
        "only_left": 1,
        "only_right": 1,
        "changed": 1,
        "duplicate_keys": 0,
    }
    assert table["changed"] == [{"key": "B1", "field": "Tube", "left": "con_t1", "right": "con_t2"}]
    assert table["only_left"][0]["key"] == "C1"
    assert table["only_left"][0]["values"]["Ct"] == 20
    assert table["only_right"][0]["key"] == "D1"
    assert table["duplicate_keys"] == []
    assert "warnings" not in diff and "containers" not in diff
    assert _bulk_calls(fake) == []
    assert fake.writes() == []


async def test_position_pairing_warns(runtime, fake) -> None:
    _first_and_rerun(fake)
    diff = await results_diff.diff_results(runtime.service, left=ENTRY_ID, right=RERUN)
    assert diff["pairing"] == "position"
    assert diff["warnings"] == [results_diff.POSITION_WARNING]
    [table] = diff["tables"]
    assert table["counts"]["matched"] == 3 and table["counts"]["changed"] == 2
    assert {change["key"] for change in table["changed"]} == {1, 2}


async def test_run_values_select_rows_on_the_same_entry(runtime, fake) -> None:
    _schema(fake)
    fake.add_rows_table(
        "docpart_runs",
        "Plate 1",
        [WELL, RUN, CT],
        [
            [text("A1"), text("research"), number(12)],
            [text("B1"), text("research"), number(15)],
            [text("A1"), text("regulatory"), number(12)],
            [text("B1"), text("regulatory"), number(16)],
            [text("C1"), text("regulatory"), number(18)],
        ],
    )
    diff = await results_diff.diff_results(
        runtime.service,
        left=f"{ENTRY_ID}:research",
        right=f"{ENTRY_ID}:regulatory",
        run_field="run_type",
        key="well",
    )
    assert diff["left"]["run_value"] == "research"
    assert diff["right"]["run_value"] == "regulatory"
    [table] = diff["tables"]
    assert table["fields"] == ["Well", "Ct"]
    assert table["counts"] == {
        "left": 2,
        "right": 3,
        "matched": 2,
        "only_left": 0,
        "only_right": 1,
        "changed": 1,
        "duplicate_keys": 0,
    }
    assert table["changed"] == [{"key": "B1", "field": "Ct", "left": 15, "right": 16}]
    assert table["only_right"][0]["row_index"] == 4
    parts_reads = [call for call in fake.calls if call.path.endswith("/parts/items")]
    assert len(parts_reads) == 1


async def test_a_container_link_key_and_duplicate_keys(runtime, fake) -> None:
    _first_and_rerun(fake)
    fake.results_parts["docpart_rerun"]["rows"][2]["cells"][1] = tube("con_t0")
    diff = await results_diff.diff_results(
        runtime.service, left=ENTRY_ID, right=RERUN, key="assayfield_tube", fields=["Well"]
    )
    [table] = diff["tables"]
    assert table["fields"] == ["Well"]
    assert table["duplicate_keys"] == [
        {
            "key": "con_t0",
            "left": 1,
            "right": 2,
            "left_row_indexes": [0],
            "right_row_indexes": [0, 2],
        }
    ]
    assert table["counts"]["duplicate_keys"] == 1
    assert table["counts"]["matched"] == 1
    assert table["changed"] == [{"key": "con_t2", "field": "Well", "left": "C1", "right": "B1"}]
    assert [row["key"] for row in table["only_left"]] == ["con_t1"]
    assert table["only_right"] == []


async def test_duplicate_keys_report_the_count_on_each_side(runtime, fake) -> None:
    """A key repeated on either side is never paired and never silently dropped."""
    _schema(fake)
    _tubes(fake, 3)
    fake.add_entry(RERUN, "EXP-2", "Synthetic rerun")
    columns = [WELL, TUBE, CT]
    fake.add_rows_table(
        "docpart_first",
        "qPCR Testing 1",
        columns,
        [
            [text("A1"), tube("con_t0"), number(1)],
            [text("A1"), tube("con_t1"), number(2)],
            [text("B1"), tube("con_t2"), number(3)],
            [text("B1"), tube("con_t2"), number(4)],
            [text("C1"), tube("con_t0"), number(5)],
        ],
    )
    fake.add_rows_table(
        "docpart_rerun",
        "qPCR Testing 1",
        columns,
        [
            [text("A1"), tube("con_t0"), number(1)],
            [text("C1"), tube("con_t0"), number(5)],
            [text("C1"), tube("con_t1"), number(6)],
            [text("D1"), tube("con_t1"), number(7)],
        ],
        entry_id=RERUN,
    )
    diff = await results_diff.diff_results(runtime.service, left=ENTRY_ID, right=RERUN, key="Well")
    [table] = diff["tables"]
    assert table["duplicate_keys"] == [
        {
            "key": "A1",
            "left": 2,
            "right": 1,
            "left_row_indexes": [0, 1],
            "right_row_indexes": [0],
        },
        {
            "key": "B1",
            "left": 2,
            "right": 0,
            "left_row_indexes": [2, 3],
            "right_row_indexes": [],
        },
        {
            "key": "C1",
            "left": 1,
            "right": 2,
            "left_row_indexes": [4],
            "right_row_indexes": [1, 2],
        },
    ]
    assert table["counts"]["duplicate_keys"] == 3
    assert table["counts"]["matched"] == 0
    assert [row["key"] for row in table["only_left"]] == []
    assert [row["key"] for row in table["only_right"]] == ["D1"]


async def test_barcodes_resolve_in_chunks_of_100_and_can_key_rows(runtime, fake) -> None:
    _schema(fake)
    _tubes(fake, 150)
    fake.add_entry(RERUN, "EXP-2", "Synthetic rerun")
    first = [[text(f"W{index}"), tube(f"con_t{index}")] for index in range(150)]
    rerun = [[text(f"W{index}"), tube(f"con_t{index}")] for index in range(150)]
    rerun[0], rerun[1] = [text("W1"), tube("con_t0")], [text("W0"), tube("con_t1")]
    fake.add_rows_table("docpart_first", "Plate", [WELL, TUBE], first)
    fake.add_rows_table("docpart_rerun", "Plate", [WELL, TUBE], rerun, entry_id=RERUN)
    diff = await results_diff.diff_results(
        runtime.service, left=ENTRY_ID, right=RERUN, key="Tube", resolve_barcodes=True
    )
    calls = _bulk_calls(fake)
    assert len(calls) == 2
    assert [len(call.params["containerIds"].split(",")) for call in calls] == [100, 50]
    assert diff["containers"] == {"requested": 150, "resolved": 150, "unresolved": []}
    [table] = diff["tables"]
    assert table["counts"]["matched"] == 150 and table["counts"]["changed"] == 2
    assert table["changed"][0] == {"key": "BC-000", "field": "Well", "left": "W0", "right": "W1"}
    assert fake.writes() == []


async def test_barcodes_show_next_to_changed_and_unpaired_tubes(runtime, fake) -> None:
    _first_and_rerun(fake)
    fake.containers = [item for item in fake.containers if item["id"] != "con_t2"]
    diff = await results_diff.diff_results(
        runtime.service, left=ENTRY_ID, right=RERUN, key="Well", resolve_barcodes=True
    )
    assert diff["containers"] == {"requested": 3, "resolved": 2, "unresolved": ["con_t2"]}
    [table] = diff["tables"]
    assert table["changed"] == [
        {
            "key": "B1",
            "field": "Tube",
            "left": "con_t1",
            "right": "con_t2",
            "left_barcode": "BC-001",
            "right_barcode": None,
        }
    ]
    assert table["only_right"][0]["barcodes"] == {"Tube": "BC-001"}


async def test_compare_blobs_reports_a_changed_blob_and_leaves_no_temp_files(
    runtime, fake, tmp_path, monkeypatch
) -> None:
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(scratch))
    _schema(fake)
    fake.add_entry(RERUN, "EXP-2", "Synthetic rerun")
    for blob_id, data in ((BLOB_A, b"first"), (BLOB_B, b"second"), (BLOB_C, b"first")):
        fake.blobs[blob_id] = {
            "id": blob_id,
            "name": "report.txt",
            "type": "RAW_FILE",
            "mimeType": "text/plain",
            "uploadStatus": "COMPLETE",
            "data": data,
        }
    fake.attachments["attachment_rerun1"] = {
        "id": "attachment_rerun1",
        "name": "report.txt",
        "mimeType": "text/plain",
        "uploadStatus": "SUCCEEDED",
        "downloadUrl": "https://downloads.example.test/files/attachment_rerun1",
        "data": b"second",
    }
    fake.add_rows_table(
        "docpart_first",
        "Reports",
        [WELL, REPORT],
        [[text("A1"), link(BLOB_A)], [text("B1"), link(BLOB_A)]],
    )
    fake.add_rows_table(
        "docpart_rerun",
        "Reports",
        [WELL, REPORT],
        [
            [text("A1"), link({"__typename": "Attachment", "id": "attachment_rerun1"})],
            [text("B1"), link(BLOB_C)],
        ],
        entry_id=RERUN,
    )
    plain = await results_diff.diff_results(runtime.service, left=ENTRY_ID, right=RERUN, key="Well")
    assert plain["tables"][0]["counts"]["changed"] == 2
    assert "blobs" not in plain["tables"][0]

    diff = await results_diff.diff_results(
        runtime.service, left=ENTRY_ID, right=RERUN, key="Well", compare_blobs=True
    )
    [table] = diff["tables"]
    assert table["counts"]["changed"] == 1
    assert table["blobs"] == {"compared": 2, "changed": 1}
    assert table["changed"] == [
        {
            "key": "A1",
            "field": "Report",
            "left": BLOB_A,
            "right": "attachment_rerun1",
            "blob_changed": True,
            "left_sha256": hashlib.sha256(b"first").hexdigest(),
            "right_sha256": hashlib.sha256(b"second").hexdigest(),
        }
    ]
    assert list(scratch.iterdir()) == []
    assert fake.writes() == []


async def test_output_writes_every_difference_and_honors_overwrite(runtime, fake, tmp_path) -> None:
    _first_and_rerun(fake)
    path = tmp_path / "diff.csv"
    diff = await results_diff.diff_results(
        runtime.service,
        left=ENTRY_ID,
        right=RERUN,
        key="Well",
        output=str(path),
        file_format="csv",
    )
    payload = path.read_bytes()
    assert diff["output"] == {
        "path": str(path),
        "format": "csv",
        "row_count": 3,
        "checksum": hashlib.sha256(payload).hexdigest(),
        "checksum_algorithm": "sha256",
    }
    lines = payload.decode().splitlines()
    assert lines[0] == ",".join(results_diff.FILE_COLUMNS)
    assert lines[1].startswith("qPCR Testing 1,changed,B1,1,1,Tube,con_t1,con_t2")
    with pytest.raises(ValidationError, match="Refusing to overwrite"):
        await results_diff.diff_results(
            runtime.service,
            left=ENTRY_ID,
            right=RERUN,
            output=str(path),
            file_format="json",
        )
    assert path.read_bytes() == payload
    replaced = await results_diff.diff_results(
        runtime.service,
        left=ENTRY_ID,
        right=RERUN,
        key="Well",
        output=str(path),
        file_format="json",
        overwrite=True,
    )
    document = json.loads(path.read_text())
    assert document["kind"] == "results-diff"
    assert document["source"]["right"]["entry_id"] == RERUN
    assert len(document["rows"]) == replaced["output"]["row_count"] == 3
    assert fake.writes() == []


async def test_table_option_restricts_the_comparison(runtime, fake) -> None:
    _first_and_rerun(fake)
    fake.add_rows_table("docpart_other", "Melt curve 1", [WELL], [[text("A1")]])
    diff = await results_diff.diff_results(
        runtime.service, left=ENTRY_ID, right=RERUN, tables=["Melt curve 1", "Missing"]
    )
    assert diff["tables"] == [] and diff["tables_only_left"] == ["Melt curve 1"]
    assert diff["tables_not_found"] == ["Missing"]
    reads = [call.path for call in fake.calls if "/rows/items" in call.path]
    assert all("docpart_other" in path for path in reads)


async def test_duplicate_table_names_are_listed_not_compared(runtime, fake) -> None:
    _first_and_rerun(fake)
    fake.add_rows_table("docpart_again", "qPCR Testing 1", [WELL], [[text("A1")]])
    diff = await results_diff.diff_results(runtime.service, left=ENTRY_ID, right=RERUN)
    assert diff["duplicate_table_names"] == {"left": ["qPCR Testing 1"], "right": []}
    assert diff["tables"] == [] and diff["tables_only_right"] == []


@pytest.mark.parametrize(
    ("arguments", "message"),
    [
        ({"left": f"{ENTRY_ID}:research"}, "run_field is required"),
        ({"left": f"{ENTRY_ID}:"}, "ENTRY or ENTRY:RUN_VALUE"),
        ({"left": " "}, "left must be an entry reference"),
        ({"key": "Well,"}, "no empty field"),
        ({"key": "Nope"}, "not a column"),
        ({"sample": 0}, "sample must be"),
        ({"output": "x.json"}, "format is required"),
        ({"file_format": "csv"}, "format applies only"),
        ({"run_field": " "}, "run_field must be"),
    ],
)
async def test_invalid_arguments_are_validation_errors(runtime, fake, arguments, message) -> None:
    _first_and_rerun(fake)
    with pytest.raises(ValidationError, match=message):
        await results_diff.diff_results(
            runtime.service, **{"left": ENTRY_ID, "right": RERUN, **arguments}
        )
    assert fake.writes() == []


async def test_an_ambiguous_field_is_a_validation_error(runtime, fake) -> None:
    _first_and_rerun(fake)
    fake.field_defs[SCHEMA][1]["systemName"] = "Ct"
    with pytest.raises(ValidationError, match="more than one column"):
        await results_diff.diff_results(
            runtime.service, left=ENTRY_ID, right=RERUN, key="Well", fields=["Ct"]
        )


def test_parse_side_keeps_a_url_scheme() -> None:
    url = "https://tenant.example.test/entries/etr_abc123"
    assert results_diff.parse_side(url, "left") == (url, None)
    assert results_diff.parse_side(f"{url}:run:2", "left") == (url, "run:2")
    assert results_diff.parse_side("EXP-1", "left") == ("EXP-1", None)


def test_run_filter_is_the_where_filter() -> None:
    def row(*values: object, status: str = "SUBMITTED", archived: bool = False) -> dict:
        cells = [{"inputValue": {"type": "text_value", "value": v}} for v in values]
        return {"submissionStatus": status, "isArchived": archived, "cells": cells}

    table = {
        "columns": ["Run", "N"],
        "rows": [{"Run": "a"}, {"Run": "b"}, {"Run": "a"}, {"Run": None}],
        "raw_rows": [
            row("a", "1"),
            row("b", "2"),
            row("a", "3", archived=True),
            {"submissionStatus": "UNSUBMITTED", "cells": [{}, {}]},
        ],
    }
    picked = results_diff._records(table, [], "Run", "a")
    assert [record["index"] for record in picked] == [0]
    # Without a run value, archived rows and the empty placeholder row are still left out.
    assert [record["index"] for record in results_diff._records(table, [], None, None)] == [0, 1]


async def test_an_unreadable_schema_still_resolves_display_names(runtime, fake) -> None:
    _first_and_rerun(fake)
    del fake.field_defs[SCHEMA]
    diff = await results_diff.diff_results(runtime.service, left=ENTRY_ID, right=RERUN, key="Well")
    assert diff["tables"][0]["counts"]["changed"] == 1


@pytest.fixture
def invoke(benchling_env, fake, monkeypatch):
    monkeypatch.setattr(cli, "Runtime", lambda environment: runtime_for(fake, environment))

    def run(*args: str):
        result = CliRunner().invoke(cli.app, [*args, "--env", "sandbox"])
        return result.exit_code, json.loads(result.stdout)

    return run


def test_cli_results_diff(invoke, fake, tmp_path) -> None:
    _first_and_rerun(fake)
    path = tmp_path / "diff.json"
    code, reply = invoke(
        "results",
        "diff",
        "--left",
        ENTRY_ID,
        "--right",
        RERUN,
        "--key",
        "Well",
        "--table",
        "qPCR Testing 1",
        "--resolve-barcodes",
        "--output",
        str(path),
        "--format",
        "json",
    )
    assert code == 0, reply
    assert reply["data"]["tables"][0]["changed"][0]["right_barcode"] == "BC-002"
    assert reply["data"]["output"]["row_count"] == 3
    code, refused = invoke(
        "results", "diff", "--left", ENTRY_ID, "--right", RERUN, "-o", str(path), "--format", "csv"
    )
    assert code != 0 and refused["ok"] is False
    assert refused["error"]["code"] == "validation_error"
    assert fake.writes() == []
