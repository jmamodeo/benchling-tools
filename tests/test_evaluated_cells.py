"""Results tables evaluated in the web app: link evaluations, unknown typenames, missing values."""

from __future__ import annotations

import json

import pytest
from conftest import runtime_for
from fake_benchling import ENTRY_ID, evaluated_results_rows
from typer.testing import CliRunner

from benchlingtk import cli, services
from benchlingtk.errors import UpstreamError
from benchlingtk.export import export_dataset

PART = "docpart_rt1"
ROWS_PATH = f"/document-part-results-table/{PART}/rows/items"


def _results_part() -> dict:
    return {
        "id": PART,
        "__typename": "DocumentPartResultsTable",
        "name": "Evaluated links",
        "resultSchema": {"__typename": "ResultSchemaRef", "id": "assaysch_links"},
    }


@pytest.fixture
def invoke(benchling_env, fake, monkeypatch):
    monkeypatch.setattr(cli, "Runtime", lambda environment: runtime_for(fake, environment))

    def run(*args: str):
        result = CliRunner().invoke(cli.app, [*args, "--env", "sandbox"])
        return result.exit_code, json.loads(result.stdout)

    return run


def test_the_working_with_tables_example_row_normalizes_offline() -> None:
    row = {
        "id": "strow_example",
        "rowIndex": 0,
        "cells": evaluated_results_rows()[0]["cells"],
    }
    normalized = services.normalize_table_row(row, require_text=False)
    first = normalized["cells"][0]
    assert first["inputValue"] == {"type": "object_link_value", "value": "seq_example1"}
    assert first["evaluationValue"] == {
        "type": "ObjectLinkCellEvaluationValue",
        "id": "seq_example1",
        "name": "Example sequence",
    }


def test_unknown_evaluation_missing_value_and_errors_are_kept() -> None:
    row = {"rowIndex": 1, "cells": evaluated_results_rows()[1]["cells"]}
    cells = services.normalize_table_row(row, require_text=False)["cells"]
    assert cells[0]["evaluationValue"] == {
        "type": "unknown",
        "typename": "FutureCellEvaluationValueKind",
    }
    assert cells[1] == {"inputValue": {"type": "object_link_value", "value": None}}
    assert cells[2]["inputValue"] == {"type": "text_value", "value": None}
    assert cells[2]["evaluationValue"] == {"type": "text_value", "value": None}
    assert cells[2]["errors"] == [
        {"errorType": "VALIDATION", "message": "Field is required", "type": "CellError"}
    ]


def test_unstructured_cells_still_need_a_text_value() -> None:
    with pytest.raises(UpstreamError) as caught:
        services.normalize_table_row(
            {"rowIndex": 2, "cells": [{"inputValue": {"__typename": "TextValue"}}]},
            require_text=True,
            part_id="docpart_text",
        )
    assert caught.value.details == {
        "part_id": "docpart_text",
        "row_index": 2,
        "cell_index": 0,
        "typename": "TextValue",
    }


async def test_results_rows_read_evaluated_link_cells(runtime, fake) -> None:
    fake.add_evaluated_results_table()
    page = await runtime.service.list_results_table_rows(PART)
    assert [row["id"] for row in page["items"]] == ["strow_e1", "strow_e2"]
    assert page["items"][0]["cells"][1]["evaluationValue"]["name"] == "Example box"
    assert page["items"][1]["cells"][0]["evaluationValue"]["type"] == "unknown"
    assert fake.writes() == []


async def test_a_rejected_cell_names_its_location_and_typename_but_no_value(runtime, fake) -> None:
    fake.add_evaluated_results_table()
    row = {
        "id": "strow_bad",
        "rowIndex": 4,
        "cells": [
            {"inputValue": {"__typename": "TextValue", "value": "keep"}},
            {"inputValue": {"__typename": "MysteryValue", "value": "secret-ish"}},
            {"inputValue": {"__typename": "TextValue", "value": "keep"}},
        ],
    }
    fake.payload_overrides.append((ROWS_PATH, 200, {"items": [row], "nextToken": ""}))
    with pytest.raises(UpstreamError, match="invalid rows") as caught:
        await runtime.service.list_results_table_rows(PART)
    assert caught.value.details == {
        "part_id": PART,
        "row_index": 4,
        "cell_index": 1,
        "typename": "MysteryValue",
    }
    assert "secret-ish" not in json.dumps(caught.value.details)


@pytest.mark.parametrize(
    "cell",
    [
        "bad",
        {"inputValue": "bad"},
        {"inputValue": {"__typename": "TextValue", "value": "ok"}, "evaluationValue": "bad"},
    ],
)
async def test_structural_cell_problems_still_raise_with_details(runtime, fake, cell) -> None:
    fake.add_evaluated_results_table()
    ok = {"inputValue": {"__typename": "TextValue", "value": "ok"}}
    row = {"rowIndex": 0, "cells": [cell, ok, ok]}
    fake.payload_overrides.append((ROWS_PATH, 200, {"items": [row], "nextToken": ""}))
    with pytest.raises(UpstreamError, match="invalid rows") as caught:
        await runtime.service.list_results_table_rows(PART)
    assert caught.value.details["part_id"] == PART
    assert caught.value.details["row_index"] == 0
    assert caught.value.details["cell_index"] == 0


async def test_a_cell_count_mismatch_names_the_part_and_row(runtime, fake) -> None:
    fake.add_evaluated_results_table()
    fake.results_parts[PART]["rows"][1]["cells"].pop()
    with pytest.raises(UpstreamError, match="invalid rows") as caught:
        await runtime.service.get_results_table_part(PART)
    assert caught.value.details == {"part_id": PART, "row_index": 1, "cells": 2, "columns": 3}


def test_entry_get_full_and_inspect_read_an_evaluated_results_table(invoke, fake) -> None:
    fake.add_evaluated_results_table()
    fake.parts.append(_results_part())
    code, full = invoke("entry", "get", ENTRY_ID, "--full")
    assert code == 0, full
    rows = full["data"]["tables"][PART]["content"]["rows"]
    assert rows[0]["cells"][0]["evaluationValue"]["type"] == "ObjectLinkCellEvaluationValue"
    code, inspected = invoke("entry", "inspect", ENTRY_ID)
    assert code == 0, inspected
    code, page = invoke("results", "rows", PART)
    assert code == 0, page
    assert len(page["data"]["items"]) == 2
    assert fake.writes() == []


async def test_results_table_export_falls_back_to_the_input_value(runtime, fake, tmp_path) -> None:
    fake.add_evaluated_results_table()
    path = tmp_path / "evaluated.json"
    summary = await export_dataset(
        runtime.service, path=str(path), kind="results-table", file_format="json", part_id=PART
    )
    assert summary["row_count"] == 2
    rows = json.loads(path.read_text(encoding="utf-8"))["rows"]
    assert rows == [
        {"Sample": "seq_example1", "Box": "box_example1", "Notes": "first"},
        {"Sample": "seq_example2", "Box": None, "Notes": None},
    ]
    assert fake.writes() == []
