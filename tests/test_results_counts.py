"""Row counts and client-side --where filters on results tables and results rows."""

from __future__ import annotations

import json

import pytest
from conftest import runtime_for
from fake_benchling import ENTRY_ID
from typer.testing import CliRunner

from benchling_tools import cli, row_filter, services, stale
from benchling_tools.errors import UpstreamError, ValidationError


@pytest.fixture
def invoke(benchling_env, fake, monkeypatch):
    monkeypatch.setattr(cli, "Runtime", lambda environment: runtime_for(fake, environment))

    def run(*args: str):
        result = CliRunner().invoke(cli.app, [*args, "--env", "sandbox"])
        return result.exit_code, json.loads(result.stdout)

    return run


def _parts_calls(fake) -> list:
    return [call for call in fake.calls if call.path.endswith("/parts/items")]


def _by_name(data: dict) -> dict[str, dict]:
    return {item["name"]: item for item in data["items"]}


def test_results_tables_add_counts_from_one_parts_listing(invoke, fake) -> None:
    fake.add_panel_tables()
    fake.parts_page_size = 100
    code, payload = invoke("results", "tables", ENTRY_ID)
    assert code == 0, payload
    tables = _by_name(payload["data"])
    first = tables["Panel Testing 1"]
    assert first["table_id"] == "strtbl_panel1"
    assert first["part_id"] == "docpart_panel1"
    assert first["row_count"] == 2
    assert first["submission_status"] == "SUBMITTED"
    assert first["placeholder_rows"] == 0
    assert tables["Panel Testing 3"]["row_count"] == 1
    assert tables["Panel Testing 3"]["placeholder_rows"] == 0
    placeholder = tables["Panel Testing 4"]
    assert placeholder["row_count"] == 1
    assert placeholder["submission_status"] == "UNSUBMITTED"
    assert placeholder["placeholder_rows"] == 1
    assert tables["Panel Testing notes"]["part_id"] == "docpart_panelnotes"
    unmatched = tables["Assay results"]
    assert unmatched["part_id"] is None
    assert unmatched["row_count"] is None
    assert "count_note" in unmatched
    assert len(_parts_calls(fake)) == 1
    # Only the placeholder candidate (UNSUBMITTED, one row) reads a row.
    row_reads = [call.path for call in fake.calls if call.path.endswith("/rows/items")]
    assert row_reads == ["/api/v3/document-part-results-table/docpart_panel4/rows/items"]
    assert fake.writes() == []


def test_no_counts_keeps_the_single_v2_read(invoke, fake) -> None:
    fake.add_panel_tables()
    code, payload = invoke("results", "tables", ENTRY_ID, "--no-counts")
    assert code == 0, payload
    assert all("row_count" not in item for item in payload["data"]["items"])
    assert _parts_calls(fake) == []
    assert not any("document-part-results-table" in call.path for call in fake.calls)
    assert [call.path for call in fake.calls if "/entries/" in call.path] == [
        f"/api/v2/entries/{ENTRY_ID}"
    ]

    code, payload = invoke("results", "tables", ENTRY_ID, "--no-counts", "--where", "x=1")
    assert code == 2
    assert payload["error"]["code"] == "validation_error"


@pytest.mark.parametrize("field", ["Processing run", "processing_run_id", "assayfield_run"])
def test_name_prefix_and_where_count_matching_rows(invoke, fake, field) -> None:
    fake.add_panel_tables()
    code, payload = invoke(
        "results", "tables", ENTRY_ID, "--name-prefix", "Panel Testing", "--where", f"{field}=run_a"
    )
    assert code == 0, payload
    data = payload["data"]
    tables = _by_name(data)
    assert list(tables) == [
        "Panel Testing 1",
        "Panel Testing 2",
        "Panel Testing 3",
        "Panel Testing 4",
        "Panel Testing notes",
    ]
    assert [tables[f"Panel Testing {n}"]["matching_rows"] for n in "1234"] == [2, 1, 0, 0]
    assert tables["Panel Testing 1"]["rows_scanned"] == 2
    assert tables["Panel Testing notes"]["matching_rows"] is None
    assert field in tables["Panel Testing notes"]["where_note"]
    assert data["rows_scanned"] == 6
    assert data["where"]["client_side"] is True
    assert "results pending" in data["where"]["note"]
    assert data["pagination"]["total"] == 5
    assert fake.writes() == []


def test_where_conditions_all_must_match_and_link_cells_match_the_linked_id(invoke, fake) -> None:
    fake.add_panel_tables()
    code, payload = invoke(
        "results",
        "tables",
        ENTRY_ID,
        "--name-prefix",
        "Panel Testing ",
        "--where",
        "Processing run=run_a",
        "--where",
        "Sample=con_tube1",
    )
    assert code == 0, payload
    tables = _by_name(payload["data"])
    assert tables["Panel Testing 1"]["matching_rows"] == 1
    assert tables["Panel Testing 2"]["matching_rows"] == 1
    assert tables["Panel Testing 3"]["matching_rows"] == 0


def test_a_where_field_no_table_has_is_a_validation_error(invoke, fake) -> None:
    fake.add_panel_tables()
    code, payload = invoke(
        "results", "tables", ENTRY_ID, "--name-prefix", "Panel", "--where", "Operator=me"
    )
    assert code == 2
    assert payload["error"]["code"] == "validation_error"
    details = payload["error"]["details"]
    assert details["unknown_fields"] == ["Operator"]
    assert "Processing run" in details["available_display_names"]
    assert "processing_run_id" in details["available_system_names"]
    assert "assayfield_run" in details["available_field_ids"]


def test_results_rows_where_returns_only_matching_rows(invoke, fake) -> None:
    fake.add_panel_tables()
    code, payload = invoke(
        "results",
        "rows",
        "strtbl_panel2",
        "--entry",
        ENTRY_ID,
        "--limit",
        "1",
        "--where",
        "processing_run_id=run_b",
    )
    assert code == 0, payload
    data = payload["data"]
    assert data["items"] == []
    assert data["scanned"] == 1
    assert data["matched"] == 0
    assert data["next_token"] == "1"

    code, payload = invoke(
        "results",
        "rows",
        "docpart_panel2",
        "--cursor",
        "1",
        "--where",
        "sample=con_tube3",
    )
    assert code == 0, payload
    data = payload["data"]
    assert [row["id"] for row in data["items"]] == ["strow_p4"]
    assert (data["scanned"], data["matched"]) == (1, 1)
    assert data["next_token"] is None
    assert fake.writes() == []


def test_results_rows_where_still_runs_the_stale_check(invoke, fake) -> None:
    fake.add_panel_tables()
    rows = fake.results_parts["docpart_panel1"]["rows"]
    rows[0]["result"] = {"__typename": "Result", "id": "result_1"}
    rows[1]["result"] = {"__typename": "Result", "id": "result_2"}
    fake.results[0]["schemaFields"] = [
        {
            "__typename": "SchemaFieldValue",
            "fieldDefinition": {"id": "assayfield_run"},
            "value": {"__typename": "TextValue", "value": "run_z"},
        }
    ]
    code, payload = invoke("results", "rows", "docpart_panel1", "--where", "Sample=con_tube1")
    assert code == 0, payload
    data = payload["data"]
    assert [row["id"] for row in data["items"]] == ["strow_p1"]
    assert data["stale_check"] == {"compared_rows": 1, "stale_rows": 1}
    assert data["warnings"] == [stale.STALE_CELL_WARNING]


def test_results_rows_unknown_where_field_lists_the_available_names(invoke, fake) -> None:
    fake.add_panel_tables()
    code, payload = invoke("results", "rows", "docpart_panel1", "--where", "Operator=me")
    assert code == 2
    details = payload["error"]["details"]
    assert details["unknown_fields"] == ["Operator"]
    assert details["available_display_names"] == ["Processing run", "Sample"]
    assert details["available_system_names"] == ["processing_run_id", "sample"]


@pytest.mark.parametrize("where", [["novalue"], ["=x"], [1], "Field=x"])
def test_malformed_where_is_a_validation_error(where) -> None:
    with pytest.raises(ValidationError):
        row_filter.parse_where(where)


def test_where_splits_at_the_first_equals_sign() -> None:
    assert row_filter.parse_where(["Note=a=b", " F =  "]) == [("Note", "a=b"), ("F", "  ")]
    assert row_filter.parse_where(None) == []


def test_ambiguous_where_field_is_a_validation_error() -> None:
    definitions = [
        {"id": "col_a", "name": "Value", "column_index": 0},
        {"id": "col_b", "name": "value", "column_index": 1},
    ]
    with pytest.raises(ValidationError, match="more than one column") as caught:
        row_filter.bind([("VALUE", "1")], definitions)
    assert len(caught.value.details["candidates"]) == 2


@pytest.mark.parametrize(
    ("row", "expected"),
    [
        ({"cells": [{"inputValue": {"type": "integer_value", "value": 12}}]}, "12"),
        ({"cells": [{"inputValue": {"type": "boolean_value", "value": True}}]}, "true"),
        ({"cells": [{"inputValue": {"type": "text_value", "value": ["a", "b"]}}]}, "b"),
        ({"cells": [{}]}, ""),
    ],
)
def test_cell_text_conversion(row, expected) -> None:
    assert row_filter.row_matches(row, [(0, expected)])


@pytest.mark.parametrize(
    "row",
    [
        {"isArchived": True, "cells": [{"inputValue": {"type": "text_value", "value": "x"}}]},
        {"cells": "bad"},
        {"cells": []},
        {"cells": [{"inputValue": {"type": "text_value", "value": "y"}}]},
        {"cells": [{"inputValue": {"type": "json_value", "value": {"k": "x"}}}]},
    ],
)
def test_rows_that_do_not_match(row) -> None:
    assert not row_filter.row_matches(row, [(0, "x")])


@pytest.mark.parametrize(
    ("row", "placeholder"),
    [
        ({"submissionStatus": "UNSUBMITTED", "cells": [{"inputValue": None}]}, True),
        ({"submissionStatus": "UNSUBMITTED", "cells": [{"inputValue": {"value": ""}}]}, True),
        ({"submissionStatus": "UNSUBMITTED", "cells": [{"inputValue": {"value": "x"}}]}, False),
        ({"submissionStatus": "UNSUBMITTED", "cells": [{"inputValue": 1}]}, False),
        ({"submissionStatus": "UNSUBMITTED", "cells": ["bad"]}, False),
        ({"submissionStatus": "UNSUBMITTED", "cells": None}, False),
        ({"submissionStatus": "SUBMITTED", "cells": []}, False),
        ("row", False),
    ],
)
def test_placeholder_rows(row, placeholder) -> None:
    assert services.is_placeholder_row(row) is placeholder


async def test_counts_reject_an_invalid_row_count(runtime, fake) -> None:
    fake.add_panel_tables()
    fake.parts[-1]["rowCount"] = "two"
    with pytest.raises(UpstreamError, match="row count"):
        await runtime.service.list_result_tables(ENTRY_ID, counts=True)


async def test_name_prefix_must_be_text(runtime, fake) -> None:
    with pytest.raises(ValidationError, match="name_prefix"):
        await runtime.service.list_result_tables(ENTRY_ID, name_prefix="")


async def test_where_skips_unmatched_tables_and_reads_schemas_once(runtime, fake) -> None:
    fake.add_panel_tables()
    result = await runtime.service.list_result_tables(ENTRY_ID, counts=True)
    await row_filter.count_tables(runtime.service, result, [("Processing run", "run_b")])
    tables = _by_name(result)
    assert tables["Assay results"]["matching_rows"] is None
    assert "where_note" in tables["Assay results"]
    assert [tables[f"Panel Testing {n}"]["matching_rows"] for n in "123"] == [0, 1, 1]
    schema_reads = [
        call.path for call in fake.calls if call.path.endswith("/result-schema/assaysch_panel")
    ]
    assert len(schema_reads) == 1


async def test_table_fields_tolerate_a_missing_schema(runtime, fake) -> None:
    columns = [
        {"name": "Run", "columnId": "col_r", "fieldDefinitionApiId": "assayfield_x"},
        "bad",
        {"name": "Plain", "columnId": "col_p"},
    ]
    definitions = await row_filter.table_fields(runtime.service, columns, "assaysch_gone", {})
    assert [item["column_index"] for item in definitions] == [0, 2]
    assert definitions[0]["systemName"] is None
    assert await row_filter.table_fields(runtime.service, None, None, {}) == []
