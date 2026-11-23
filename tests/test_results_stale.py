"""Stale results-table cells: a cell that disagrees with its linked result record."""

from __future__ import annotations

import json

import pytest
from conftest import runtime_for
from fake_benchling import ENTRY_ID
from typer.testing import CliRunner

from benchling_tools import cli, stale

PART = "docpart_rt1"
FIELD = "assayfield_ct"


def _field(value, field_id: str = FIELD) -> dict:
    return {
        "__typename": "SchemaFieldValue",
        "fieldDefinition": {"id": field_id, "__typename": "SchemaFieldDefinitionRef"},
        "value": value,
    }


def _linked_table(fake, result_value) -> None:
    """Results table whose Ct column maps to FIELD; row 1 links result_1 and shows 12."""
    fake.add_results_table()
    fake.results_parts[PART]["columnDefinitions"][0]["fieldDefinitionApiId"] = FIELD
    fake.results[0]["schemaFields"] = [_field(result_value)]
    fake.parts.append(
        {
            "id": PART,
            "__typename": "DocumentPartResultsTable",
            "name": "qPCR, plate 1",
            "resultSchema": {"__typename": "ResultSchemaRef", "id": "assaysch_abc123"},
        }
    )


def _result_reads(fake) -> list[str]:
    return [call.path for call in fake.calls if "/result/result_" in call.path]


async def test_agreeing_cells_carry_no_warning(runtime, fake) -> None:
    _linked_table(fake, {"__typename": "IntegerValue", "value": 12})
    page = await runtime.service.list_results_table_rows(PART)
    page = await stale.annotate_rows(runtime.service, page)
    assert page["stale_check"] == {"compared_rows": 1, "stale_rows": 0}
    assert "warnings" not in page
    assert all("warning" not in row and "stale_cells" not in row for row in page["items"])
    assert fake.writes() == []


async def test_disagreeing_cells_warn_to_re_sync_the_table(runtime, fake) -> None:
    _linked_table(fake, {"__typename": "IntegerValue", "value": 15})
    page = await runtime.service.list_results_table_rows(PART)
    page = await stale.annotate_rows(runtime.service, page)
    assert page["stale_check"] == {"compared_rows": 1, "stale_rows": 1}
    assert page["warnings"] == [stale.STALE_CELL_WARNING]
    assert "Re-sync table" in stale.STALE_CELL_WARNING
    assert "plug" not in stale.STALE_CELL_WARNING
    first, *others = page["items"]
    assert first["warning"] == stale.STALE_CELL_WARNING
    assert first["stale_cells"] == [
        {
            "column_index": 0,
            "column_id": "col_ct",
            "column_name": "Ct",
            "field_definition_id": FIELD,
            "table_value": 12,
            "result_value": 15,
        }
    ]
    assert all("warning" not in row for row in others)
    assert _result_reads(fake) == ["/api/v3/result/result_1"]
    assert fake.writes() == []


async def test_rows_with_unsubmitted_changes_are_not_compared(runtime, fake) -> None:
    _linked_table(fake, {"__typename": "IntegerValue", "value": 15})
    fake.results_parts[PART]["rows"][0]["submissionStatus"] = "HAS_UNSUBMITTED_CHANGES"
    page = await stale.annotate_rows(
        runtime.service, await runtime.service.list_results_table_rows(PART)
    )
    assert page["stale_check"] == {"compared_rows": 0, "stale_rows": 0}
    assert "warnings" not in page
    assert _result_reads(fake) == []


async def test_no_result_is_read_when_no_column_maps_to_a_field(runtime, fake) -> None:
    fake.add_results_table()
    fake.results[0]["schemaFields"] = [_field({"__typename": "IntegerValue", "value": 15})]
    page = await stale.annotate_rows(
        runtime.service, await runtime.service.list_results_table_rows(PART)
    )
    assert page["stale_check"] == {"compared_rows": 0, "stale_rows": 0}
    assert _result_reads(fake) == []


async def test_a_result_that_cannot_be_read_is_listed_not_raised(runtime, fake) -> None:
    _linked_table(fake, {"__typename": "IntegerValue", "value": 12})
    fake.results = fake.results[1:]
    page = await stale.annotate_rows(
        runtime.service, await runtime.service.list_results_table_rows(PART)
    )
    assert page["stale_check"] == {
        "compared_rows": 0,
        "stale_rows": 0,
        "unreadable_results": ["result_1"],
    }


@pytest.mark.parametrize(
    ("table", "record", "same"),
    [
        (12, 12, True),
        ("1.5", 1.5, True),
        (12, 12.0, True),
        (None, "", True),
        ("", None, True),
        ([], None, True),
        ("seq_a", "seq_a", True),
        (["b", "a"], ["a", "b"], True),
        ([1, "2"], [1.0, 2], True),
        (12, 15, False),
        ("old", "new", False),
        (None, "new", False),
        (True, 1, False),
        (["a"], ["a", "b"], False),
    ],
)
def test_cell_and_record_values_compare_by_what_they_show(table, record, same) -> None:
    assert stale.same_value(table, record) is same


def test_a_column_without_a_returned_field_is_not_compared() -> None:
    fields = stale.column_fields(
        [{"fieldDefinitionApiId": FIELD, "name": "Ct"}, {"name": "System"}, "bad"]
    )
    assert list(fields) == [0]
    row = {"cells": [{"inputValue": {"type": "integer_value", "value": 12}}]}
    assert stale.stale_cells(row, fields, {"id": "result_x"}) is None
    other = {"id": "result_x", "schemaFields": [_field({"value": 1}, "assayfield_other")]}
    assert stale.stale_cells(row, fields, other) is None


def test_a_link_cell_matches_the_linked_entity_id() -> None:
    fields = stale.column_fields([{"fieldDefinitionApiId": FIELD}])
    row = {"cells": [{"inputValue": {"type": "object_link_value", "value": "seq_a"}}]}
    record = {
        "id": "result_x",
        "schemaFields": [{**_field(None), "linkedEntityId": "seq_a"}],
    }
    assert stale.stale_cells(row, fields, record) == []
    own = {"id": "result_x", "schemaFields": [{**_field(None), "linkedEntityId": "result_x"}]}
    assert stale.stale_cells(row, fields, own)[0]["result_value"] is None


async def test_results_get_reports_a_stale_linked_row(runtime, fake) -> None:
    _linked_table(fake, {"__typename": "IntegerValue", "value": 15})
    record = await stale.check_result_tables(
        runtime.service, await runtime.service.get_result("result_1")
    )
    assert record["warnings"] == [stale.STALE_CELL_WARNING]
    check = record["table_check"]
    assert check["status"] == "stale"
    assert check["entry_id"] == ENTRY_ID
    assert check["part_id"] == PART
    assert check["row_id"] == "row_1"
    assert check["row_index"] == 0
    assert check["warning"] == stale.STALE_CELL_WARNING
    assert check["stale_cells"][0]["table_value"] == 12
    assert check["stale_cells"][0]["result_value"] == 15
    assert fake.writes() == []


async def _unevaluated(runtime, fake, value) -> tuple[dict, dict]:
    _linked_table(fake, {"__typename": "IntegerValue", "value": value})
    fake.results_parts[PART]["rows"][0]["submissionStatus"] = "UNEVALUATED"
    page = await stale.annotate_rows(
        runtime.service, await runtime.service.list_results_table_rows(PART)
    )
    record = await stale.check_result_tables(
        runtime.service, await runtime.service.get_result_by_any_id("result_1")
    )
    return page, record


async def test_an_unevaluated_linked_row_that_agrees_is_in_sync(runtime, fake) -> None:
    page, record = await _unevaluated(runtime, fake, 12)
    assert page["items"][0]["submissionStatus"] == "UNEVALUATED"
    assert page["stale_check"] == {"compared_rows": 1, "stale_rows": 0}
    assert "warnings" not in page and "warning" not in page["items"][0]
    assert record["table_check"]["status"] == "in_sync"
    assert "warnings" not in record
    assert fake.writes() == []


async def test_an_unevaluated_linked_row_that_disagrees_is_stale(runtime, fake) -> None:
    page, record = await _unevaluated(runtime, fake, 15)
    assert page["stale_check"] == {"compared_rows": 1, "stale_rows": 1}
    assert page["warnings"] == [stale.STALE_CELL_WARNING]
    assert page["items"][0]["stale_cells"][0]["result_value"] == 15
    assert record["table_check"]["status"] == "stale"
    assert record["warnings"] == [stale.STALE_CELL_WARNING]
    assert "Re-sync table" in record["table_check"]["warning"]
    assert fake.writes() == []


@pytest.mark.parametrize(
    ("row", "comparable"),
    [
        ({"submissionStatus": "SUBMITTED"}, True),
        ({"submissionStatus": "UNEVALUATED"}, True),
        ({}, True),
        ({"submissionStatus": "UNSUBMITTED"}, False),
        ({"submissionStatus": "HAS_UNSUBMITTED_CHANGES"}, False),
        ({"submissionStatus": "SUBMITTED", "isArchived": True}, False),
        ({"submissionStatus": "UNEVALUATED", "isArchived": True}, False),
    ],
)
def test_only_archived_or_unsubmitted_rows_are_skipped(row, comparable) -> None:
    assert stale.row_is_comparable(row) is comparable


async def test_results_get_reports_an_in_sync_linked_row(runtime, fake) -> None:
    _linked_table(fake, {"__typename": "IntegerValue", "value": 12})
    record = await stale.check_result_tables(
        runtime.service, await runtime.service.get_result("result_1")
    )
    assert record["table_check"]["status"] == "in_sync"
    assert "warnings" not in record
    assert "stale_cells" not in record["table_check"]


async def test_results_get_without_a_linking_row_or_entry(runtime, fake) -> None:
    _linked_table(fake, {"__typename": "IntegerValue", "value": 15})
    unlinked = await stale.check_result_tables(
        runtime.service, await runtime.service.get_result("result_2")
    )
    assert unlinked["table_check"] == {"entry_id": ENTRY_ID, "status": "no_table_row"}
    assert "warnings" not in unlinked

    pending = await stale.check_result_tables(runtime.service, {"id": "result_p", "entry": None})
    assert pending["table_check"] == {"status": "not_on_entry"}


async def test_results_get_does_not_compare_a_row_with_unsubmitted_changes(runtime, fake) -> None:
    _linked_table(fake, {"__typename": "IntegerValue", "value": 15})
    fake.results_parts[PART]["rows"][0]["submissionStatus"] = "HAS_UNSUBMITTED_CHANGES"
    record = await stale.check_result_tables(
        runtime.service, await runtime.service.get_result("result_1")
    )
    check = record["table_check"]
    assert check["status"] == "not_compared"
    assert check["submission_status"] == "HAS_UNSUBMITTED_CHANGES"
    assert "warnings" not in record

    fake.results_parts[PART]["rows"][0]["submissionStatus"] = "SUBMITTED"
    fake.results[0]["schemaFields"] = [_field(15, "assayfield_other")]
    record = await stale.check_result_tables(
        runtime.service, await runtime.service.get_result("result_1")
    )
    assert record["table_check"]["status"] == "not_compared"


async def test_results_get_reports_an_unreadable_entry_without_failing(runtime, fake) -> None:
    _linked_table(fake, {"__typename": "IntegerValue", "value": 15})
    fake.fail("GET", "/parts/items", 404)
    record = await stale.check_result_tables(
        runtime.service, await runtime.service.get_result("result_1")
    )
    assert record["id"] == "result_1"
    assert record["table_check"] == {
        "entry_id": ENTRY_ID,
        "status": "unavailable",
        "reason": "not_found",
    }


async def test_results_get_skips_tables_of_another_schema(runtime, fake) -> None:
    _linked_table(fake, {"__typename": "IntegerValue", "value": 15})
    fake.parts[-1]["resultSchema"]["id"] = "assaysch_other"
    record = await stale.check_result_tables(
        runtime.service, await runtime.service.get_result("result_1")
    )
    assert record["table_check"]["status"] == "no_table_row"
    assert not any("document-part-results-table" in call.path for call in fake.calls)


@pytest.fixture
def invoke(benchling_env, fake, monkeypatch):
    monkeypatch.setattr(cli, "Runtime", lambda environment: runtime_for(fake, environment))

    def run(*args: str):
        result = CliRunner().invoke(cli.app, [*args, "--env", "sandbox"])
        return result.exit_code, json.loads(result.stdout)

    return run


def test_cli_results_rows_and_get_carry_the_re_sync_warning(invoke, fake) -> None:
    _linked_table(fake, {"__typename": "IntegerValue", "value": 15})
    code, rows = invoke("results", "rows", PART)
    assert code == 0, rows
    assert rows["data"]["warnings"] == [stale.STALE_CELL_WARNING]
    assert rows["data"]["items"][0]["stale_cells"][0]["result_value"] == 15

    code, got = invoke("results", "get", "result_1")
    assert code == 0, got
    assert got["data"]["id"] == "result_1"
    assert got["data"]["table_check"]["status"] == "stale"
    assert got["data"]["warnings"] == [stale.STALE_CELL_WARNING]
    assert fake.writes() == []


def test_cli_results_rows_in_sync_has_no_warning(invoke, fake) -> None:
    _linked_table(fake, {"__typename": "IntegerValue", "value": 12})
    code, rows = invoke("results", "rows", PART)
    assert code == 0, rows
    assert "warnings" not in rows["data"]
    assert rows["data"]["stale_check"] == {"compared_rows": 1, "stale_rows": 0}
