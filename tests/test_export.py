"""Export writes JSON or CSV and returns a summary, against the fake API."""

from __future__ import annotations

import csv
import hashlib
import json
import os
from pathlib import Path
from typing import Any

import pytest
from conftest import runtime_for
from fake_benchling import ENTRY_ID, TABLE_PART, TENANT, text_cell
from mcp.shared.memory import create_connected_server_and_client_session as Client
from typer.testing import CliRunner

from benchling_tools import cli
from benchling_tools.errors import NotFoundError, UpstreamError, ValidationError
from benchling_tools.export import (
    _assay_columns,
    _assay_row,
    _cell_value,
    _table_row,
    export_dataset,
)
from benchling_tools.mcp_server import build_mcp_server

SUMMARY_KEYS = {
    "kind",
    "format",
    "path",
    "row_count",
    "columns",
    "checksum",
    "checksum_algorithm",
    "source",
}


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _partials(directory: Path) -> list[Path]:
    return list(directory.glob(".*.partial"))


async def _export(runtime, path: Path, **kwargs):
    return await export_dataset(runtime.service, path=str(path), **kwargs)


def _row_calls(fake, suffix: str = "/rows/items") -> list:
    return [call for call in fake.calls if call.path.endswith(suffix)]


async def test_table_export_writes_the_file_and_returns_only_a_summary(
    runtime, fake, tmp_path
) -> None:
    fake.table_page_size = 1
    json_path = tmp_path / "measurements.json"
    summary = await _export(
        runtime,
        json_path,
        kind="TABLE",
        file_format=" Json ",
        part_id=f"  {TABLE_PART}\n",
    )
    assert set(summary) == SUMMARY_KEYS
    assert summary["kind"] == "table"
    assert summary["format"] == "json"
    assert summary["path"] == str(json_path.resolve())
    assert summary["row_count"] == 3
    assert summary["columns"] == ["Sample", "Value"]
    assert summary["checksum"] == _sha256(json_path)
    assert summary["checksum_algorithm"] == "sha256"
    assert summary["source"] == {
        "part_id": TABLE_PART,
        "name": "Measurements",
        "version_id": "ver_1",
    }
    document = json.loads(json_path.read_text(encoding="utf-8"))
    assert document["rows"] == [
        {"Sample": "A", "Value": "1"},
        {"Sample": "B", "Value": ""},
        {"Sample": "C", "Value": "=SUM(A1:A2)"},
    ]
    dumped = json.dumps(summary)
    assert "=SUM(A1:A2)" not in dumped
    assert "rows" not in summary
    assert fake.writes() == []
    assert [call.params.get("nextToken") for call in _row_calls(fake)] == [None, "1", "2"]
    assert _partials(tmp_path) == []

    csv_path = tmp_path / "measurements.csv"
    csv_summary = await _export(
        runtime, csv_path, kind="table", file_format="csv", part_id=TABLE_PART
    )
    assert csv_summary["row_count"] == 3
    assert csv_summary["columns"] == summary["columns"]
    assert csv_summary["checksum"] == _sha256(csv_path)
    assert csv_summary["checksum"] != summary["checksum"]
    assert list(csv.reader(csv_path.read_text(encoding="utf-8").splitlines())) == [
        ["Sample", "Value"],
        ["A", "1"],
        ["B", ""],
        ["C", "=SUM(A1:A2)"],
    ]
    assert "=SUM(A1:A2)" not in json.dumps(csv_summary)


async def test_table_export_uses_column_names_and_the_evaluation(runtime, fake, tmp_path) -> None:
    fake.table_content["name"] = ""
    fake.table_content["columnDefinitions"] = [
        {"name": "Sample"},
        {"name": " ", "systemName": "from_system"},
        {"columnId": "col_x"},
        {"name": " ", "systemName": " ", "columnId": " "},
        "nope",
        {"name": "Sample"},
    ]
    fake.table_content["rows"] = [
        {
            "cells": [
                text_cell("alpha", evaluatedValue={"type": "text_value", "value": "shown-sample"}),
                text_cell("µg/mL"),
            ]
        },
        {"cells": []},
    ]
    path = tmp_path / "wide.json"
    summary = await _export(runtime, path, kind="table", file_format="json", part_id=TABLE_PART)
    assert summary["columns"] == [
        "Sample",
        "from_system",
        "col_x",
        "column_3",
        "column_4",
        "Sample__2",
    ]
    assert summary["source"] == {"part_id": TABLE_PART, "version_id": "ver_1"}
    document = json.loads(path.read_text(encoding="utf-8"))
    assert document["rows"][0]["Sample"] == "shown-sample"
    assert document["rows"][0]["from_system"] == "µg/mL"
    assert document["rows"][0]["Sample__2"] is None
    assert document["rows"][1] == {column: None for column in summary["columns"]}
    assert "shown-sample" not in json.dumps(summary)
    assert "µg/mL" not in json.dumps(summary)
    assert "µg/mL" in path.read_text(encoding="utf-8")


async def test_empty_table_export_writes_headers_and_no_rows(runtime, fake, tmp_path) -> None:
    fake.table_content["rows"] = []
    path = tmp_path / "empty.csv"
    summary = await _export(runtime, path, kind="table", file_format="csv", part_id=TABLE_PART)
    assert summary["row_count"] == 0
    assert summary["columns"] == ["Sample", "Value"]
    assert path.read_text(encoding="utf-8") == "Sample,Value\n"
    assert fake.writes() == []


async def test_non_list_column_definitions_fall_back_to_positions(runtime, fake, tmp_path) -> None:
    fake.table_content["columnDefinitions"] = "Ct"
    fake.table_content["rows"] = [{"cells": [text_cell("only")]}]
    summary = await _export(
        runtime, tmp_path / "fallback.csv", kind="table", file_format="csv", part_id=TABLE_PART
    )
    assert summary["columns"] == ["column_0"]
    assert summary["row_count"] == 1


async def test_results_table_export_follows_short_pages(runtime, fake, tmp_path) -> None:
    fake.add_results_table()
    fake.results_parts["docpart_rt1"]["resultSchema"] = None
    fake.results_row_page_size = 1
    path = tmp_path / "plate.csv"
    summary = await _export(
        runtime,
        path,
        kind="Results_Table",
        file_format="csv",
        part_id="docpart_rt1",
    )
    assert summary["kind"] == "results-table"
    assert summary["row_count"] == 3
    assert summary["columns"] == ["Ct"]
    assert summary["source"] == {
        "part_id": "docpart_rt1",
        "name": "qPCR, plate 1",
        "version_id": "ver_1",
    }
    assert "schema_id" not in summary["source"]
    assert list(csv.reader(path.read_text(encoding="utf-8").splitlines())) == [
        ["Ct"],
        ["12"],
        ["1.5"],
        ["ok"],
    ]
    assert "1.5" not in json.dumps(summary)
    assert "strow_1" not in json.dumps(summary)
    assert [call.params.get("nextToken") for call in _row_calls(fake)] == [None, "1", "2"]
    assert fake.writes() == []
    assert _partials(tmp_path) == []


async def test_results_table_export_follows_the_outer_page(runtime, fake, tmp_path) -> None:
    fake.add_results_table()
    fake.results_parts["docpart_rt1"]["rows"] = [
        {
            "id": f"strow_{index}",
            "cells": [{"inputValue": {"__typename": "IntegerValue", "value": index}}],
        }
        for index in range(101)
    ]
    path = tmp_path / "long.json"
    summary = await _export(
        runtime, path, kind="results-table", file_format="json", part_id="docpart_rt1"
    )
    assert summary["row_count"] == 101
    assert summary["source"]["schema_id"] == "assaysch_abc123"
    rows = json.loads(path.read_text(encoding="utf-8"))["rows"]
    assert rows[0] == {"Ct": 0}
    assert rows[-1] == {"Ct": 100}
    assert "strow_100" not in json.dumps(summary)
    tokens = [call.params.get("nextToken") for call in _row_calls(fake)]
    assert tokens == [None, "100"]
    assert all(call.method == "GET" for call in fake.calls)


async def test_results_table_export_rejects_a_row_count_mismatch(runtime, fake, tmp_path) -> None:
    fake.add_results_table()
    fake.payload_overrides.append(
        (
            "/document-part-results-table/docpart_rt1",
            200,
            {
                "id": "docpart_rt1",
                "name": "qPCR, plate 1",
                "rowCount": 9,
                "columnDefinitions": [{"name": "Ct"}],
                "rows": f"{TENANT}/api/v3/document-part-results-table/docpart_rt1/rows/items",
                "versionMetadata": {"versionId": "ver_1"},
            },
        )
    )
    path = tmp_path / "bad.csv"
    with pytest.raises(UpstreamError, match="row count"):
        await _export(runtime, path, kind="results-table", file_format="csv", part_id="docpart_rt1")
    assert not path.exists()
    assert _partials(tmp_path) == []


async def test_assay_export_flattens_fields_and_follows_pages(runtime, fake, tmp_path) -> None:
    fake.results = [
        {
            "id": "result_1",
            "entryId": ENTRY_ID,
            "schemaId": "assaysch_abc123",
            "createdAt": "2026-01-01T00:00:00Z",
            "archived": False,
            "fields": {
                "": {"value": "skip-me"},
                "ct": {"value": 12},
                "note": {"textValue": "hello-note"},
                "shown": {"displayValue": "shown-value"},
                "flag": {"value": False},
                "tags": [{"value": "alpha"}, {"value": "beta"}],
                "extra": {"unexpected": 1},
                "plain": 4,
            },
            "entry": {"id": ENTRY_ID},
        },
        {
            "id": "result_2",
            "entryId": ENTRY_ID,
            "schemaId": "assaysch_abc123",
            "fields": {"ct": {"value": 13}, "flag": {"value": True}},
        },
    ]
    fake.results_page_size = 1
    path = tmp_path / "assay.csv"
    summary = await _export(runtime, path, kind="assay-results", file_format="csv")
    assert summary["columns"] == [
        "id",
        "entryId",
        "schemaId",
        "createdAt",
        "ct",
        "note",
        "shown",
        "flag",
        "tags",
        "extra",
        "plain",
        "archived",
    ]
    assert summary["row_count"] == 2
    assert summary["source"] == {}
    rows = list(csv.reader(path.read_text(encoding="utf-8").splitlines()))
    assert rows[1] == [
        "result_1",
        ENTRY_ID,
        "assaysch_abc123",
        "2026-01-01T00:00:00Z",
        "12",
        "hello-note",
        "shown-value",
        "false",
        '["alpha","beta"]',
        '{"unexpected":1}',
        "4",
        "false",
    ]
    assert rows[2][4] == "13"
    assert rows[2][7] == "true"
    assert rows[2][3] == ""
    assert "skip-me" not in path.read_text(encoding="utf-8")
    assert "hello-note" not in json.dumps(summary)
    assert "entry" not in summary["columns"]
    result_calls = [call for call in fake.calls if call.path.endswith("/result/items")]
    assert [call.params.get("nextToken") for call in result_calls] == [None, "1"]
    assert fake.writes() == []


async def test_assay_export_sends_resolved_filters(runtime, fake, tmp_path) -> None:
    path = tmp_path / "filtered.json"
    summary = await _export(
        runtime,
        path,
        kind="assay_results",
        file_format="json",
        entry="EXP-1",
        schema_id="assaysch_abc123",
    )
    assert summary["row_count"] == 3
    assert summary["columns"] == ["id", "entryId", "schemaId"]
    assert summary["source"] == {"entry_id": ENTRY_ID, "schema_id": "assaysch_abc123"}
    assert json.loads(path.read_text(encoding="utf-8"))["rows"][0]["id"] == "result_1"
    assert "result_1" not in json.dumps(summary)
    (call,) = [call for call in fake.calls if call.path.endswith("/result/items")]
    assert call.params["entry.eq"] == ENTRY_ID
    assert call.params["schema.eq"] == "assaysch_abc123"
    assert call.params["pageSize"] == "100"

    empty = tmp_path / "empty.json"
    empty_summary = await _export(
        runtime,
        empty,
        kind="assay-results",
        file_format="json",
        schema_id="assaysch_none",
    )
    assert empty_summary["row_count"] == 0
    assert empty_summary["columns"] == []
    assert empty_summary["source"] == {"schema_id": "assaysch_none"}
    assert json.loads(empty.read_text(encoding="utf-8"))["rows"] == []


def _link(typename: str, linked_id: str) -> dict[str, Any]:
    return {"__typename": typename, "value": {"__typename": "Container", "id": linked_id}}


def _text(value: str) -> dict[str, Any]:
    return {"__typename": "TextValue", "value": value}


def _schema_field(field_id: str, value: Any, *, linked: str | None = None) -> dict[str, Any]:
    item: dict[str, Any] = {
        "__typename": "SchemaFieldValue",
        "id": f"sfv_{field_id}",
        "fieldDefinition": {"__typename": "SchemaFieldDefinitionRef", "id": field_id},
        "value": value,
    }
    if linked is not None:
        item["linkedEntityId"] = linked
    return item


_RESULT_FIELDS = [
    ("assayfield_tube", "Tube", "tube", "AnyEntityLinkFieldDefinition"),
    ("assayfield_contents", "Contents", "contents", "TextFieldDefinition"),
    ("assayfield_registry", "Registry ID", "registry_id", "TextFieldDefinition"),
    ("assayfield_entry", "Entry", "entry", "EntryLinkFieldDefinition"),
    ("assayfield_quantity", "Quantity", "quantity", "DecimalFieldDefinition"),
    ("assayfield_unit", "Quantity Unit", "quantity_unit", "TextFieldDefinition"),
]


def _install_result_fields(fake) -> None:
    fake.field_defs["assaysch_abc123"] = [
        {
            "id": field_id,
            "name": name,
            "systemName": system_name,
            "__typename": typename,
            "derivationType": "DIRECT",
        }
        for field_id, name, system_name, typename in _RESULT_FIELDS
    ]


def _v3_result(result_id: str, fields: list[dict[str, Any]]) -> dict[str, Any]:
    """A V3 Result: scalar metadata plus schemaFields, and no top-level fields map."""
    return {
        "id": result_id,
        "createdAt": "2026-01-01T00:00:00Z",
        "modifiedAt": "2026-01-02T00:00:00Z",
        "archived": False,
        "archiveReason": None,
        "__typename": "Result",
        "schema": {"__typename": "ResultSchemaRef", "id": "assaysch_abc123"},
        "entry": {"__typename": "Entry", "id": ENTRY_ID},
        "project": {"__typename": "Project", "id": "src_abc123"},
        "schemaFields": fields,
    }


async def test_assay_export_flattens_v3_schema_fields(runtime, fake, tmp_path) -> None:
    """schemaFields is an array of field-definition refs, not top-level keys."""
    _install_result_fields(fake)
    fake.results = [
        _v3_result(
            "result_1",
            [
                _schema_field("assayfield_tube", _link("ObjectLinkValue", "con_abc123")),
                _schema_field("assayfield_contents", _text("lysis buffer")),
                _schema_field("assayfield_registry", _text("REG-1")),
                _schema_field("assayfield_entry", None, linked=ENTRY_ID),
                _schema_field(
                    "assayfield_quantity", {"__typename": "DecimalValue", "value": "2.5"}
                ),
                _schema_field("assayfield_unit", _text("mL")),
            ],
        ),
        _v3_result(
            "result_2",
            [
                _schema_field(
                    "assayfield_tube",
                    {
                        "__typename": "ObjectLinkListValue",
                        "value": [
                            {"__typename": "Container", "id": "con_abc123"},
                            {"__typename": "Container", "id": "con_def456"},
                        ],
                    },
                ),
                _schema_field("assayfield_contents", _text("water")),
                _schema_field("assayfield_registry", None),
                _schema_field(
                    "assayfield_entry",
                    _link("ObjectLinkValue", ENTRY_ID),
                ),
                _schema_field("assayfield_quantity", {"__typename": "DecimalValue", "value": "0"}),
                _schema_field("assayfield_unit", {"__typename": "BooleanValue", "value": False}),
            ],
        ),
    ]
    path = tmp_path / "assay.csv"
    summary = await _export(
        runtime,
        path,
        kind="assay-results",
        file_format="csv",
        entry=ENTRY_ID,
        schema_id="assaysch_abc123",
    )
    assert summary["columns"] == [
        "id",
        "createdAt",
        "modifiedAt",
        "Tube",
        "Contents",
        "Registry ID",
        "Entry",
        "Quantity",
        "Quantity Unit",
        "archived",
        "archiveReason",
        "__typename",
    ]
    assert summary["row_count"] == 2
    assert "lysis buffer" not in json.dumps(summary)
    assert "con_abc123" not in json.dumps(summary)
    rows = list(csv.reader(path.read_text(encoding="utf-8").splitlines()))
    assert rows[1] == [
        "result_1",
        "2026-01-01T00:00:00Z",
        "2026-01-02T00:00:00Z",
        "con_abc123",
        "lysis buffer",
        "REG-1",
        ENTRY_ID,
        "2.5",
        "mL",
        "false",
        "",
        "Result",
    ]
    assert rows[2][3] == '["con_abc123","con_def456"]'
    assert rows[2][5] == ""
    assert rows[2][6] == ENTRY_ID
    assert rows[2][8] == "false"
    assert "entry" not in summary["columns"]
    assert "schema" not in summary["columns"]
    assert "schemaFields" not in summary["columns"]
    definition_calls = [call for call in fake.calls if "/field-definitions/items" in call.path]
    assert [call.params.get("nextToken") for call in definition_calls] == [None, "5"]
    assert fake.writes() == []

    json_path = tmp_path / "assay.json"
    json_summary = await _export(
        runtime,
        json_path,
        kind="assay-results",
        file_format="json",
        schema_id="assaysch_abc123",
    )
    document = json.loads(json_path.read_text(encoding="utf-8"))
    assert document["rows"][0]["Tube"] == "con_abc123"
    assert document["rows"][0]["Quantity"] == "2.5"
    assert document["columns"] == json_summary["columns"]
    assert "lysis buffer" not in json.dumps(json_summary)


async def test_assay_export_leaves_unset_fields_empty(runtime, fake, tmp_path) -> None:
    """A null schema field stays null. The result's own id is not a field value."""
    _install_result_fields(fake)
    fake.field_defs["assaysch_abc123"].extend(
        [
            {"name": "No id"},
            {"id": "", "name": "Blank", "__typename": "TextFieldDefinition"},
            {"id": "assayfield_mystery"},
            {
                "id": "assayfield_tube",
                "name": "Tube again",
                "__typename": "AnyEntityLinkFieldDefinition",
            },
        ]
    )
    fake.results = [
        _v3_result(
            "result_1",
            [
                _schema_field(
                    "assayfield_tube",
                    _link("ObjectLinkValue", "con_abc123"),
                    linked="result_1",
                ),
                _schema_field("assayfield_contents", None, linked="result_1"),
                _schema_field("assayfield_registry", None, linked="bfi_other"),
                _schema_field("assayfield_entry", None, linked=ENTRY_ID),
                _schema_field("assayfield_quantity", None, linked="result_1"),
                _schema_field(
                    "assayfield_unit",
                    {"__typename": "TextValue", "value": ""},
                    linked="result_1",
                ),
            ],
        ),
        _v3_result(
            "result_2",
            [
                _schema_field("assayfield_tube", None, linked="result_2"),
                _schema_field("assayfield_contents", _text("water")),
                _schema_field("assayfield_registry", None),
                _schema_field("assayfield_entry", None, linked="result_2"),
                _schema_field("assayfield_quantity", None, linked="bfi_other"),
                _schema_field("assayfield_unit", {"__typename": "BooleanValue", "value": False}),
                _schema_field("assayfield_mystery", None, linked="ent_mystery"),
            ],
        ),
    ]
    path = tmp_path / "blank.csv"
    summary = await _export(
        runtime,
        path,
        kind="assay-results",
        file_format="csv",
        entry=ENTRY_ID,
        schema_id="assaysch_abc123",
    )
    assert summary["columns"] == [
        "id",
        "createdAt",
        "modifiedAt",
        "Tube",
        "Contents",
        "Registry ID",
        "Entry",
        "Quantity",
        "Quantity Unit",
        "assayfield_mystery",
        "archived",
        "archiveReason",
        "__typename",
    ]
    rows = list(csv.reader(path.read_text(encoding="utf-8").splitlines()))
    assert rows[1] == [
        "result_1",
        "2026-01-01T00:00:00Z",
        "2026-01-02T00:00:00Z",
        "con_abc123",
        "",
        "",
        ENTRY_ID,
        "",
        "",
        "",
        "false",
        "",
        "Result",
    ]
    assert rows[2][3:10] == ["", "water", "", "", "", "false", "ent_mystery"]
    assert "result_1" not in rows[1][4:10]
    assert "result_2" not in rows[2][3:10]
    json_path = tmp_path / "blank.json"
    await _export(
        runtime,
        json_path,
        kind="assay-results",
        file_format="json",
        schema_id="assaysch_abc123",
    )
    document = json.loads(json_path.read_text(encoding="utf-8"))
    assert document["rows"][0]["Tube"] == "con_abc123"
    assert document["rows"][0]["Contents"] is None
    assert document["rows"][0]["Registry ID"] is None
    assert document["rows"][0]["Entry"] == ENTRY_ID
    assert document["rows"][0]["Quantity"] is None
    assert document["rows"][0]["Quantity Unit"] == ""
    assert document["rows"][1]["Tube"] is None
    assert document["rows"][1]["Entry"] is None
    assert document["rows"][1]["Quantity"] is None
    assert document["rows"][1]["Quantity Unit"] is False
    assert document["rows"][1]["assayfield_mystery"] == "ent_mystery"
    assert "con_abc123" not in json.dumps(summary)


async def test_results_table_export_flattens_link_cells(runtime, fake, tmp_path) -> None:
    """A listed results table writes link cells as ids, in JSON and CSV."""
    fake.add_results_table("docpart_links")
    spec = fake.results_parts["docpart_links"]
    spec["columnDefinitions"] = [
        {"name": "Tube"},
        {"name": "Contents"},
        {"name": "Quantity"},
    ]
    spec["rows"] = [
        {
            "id": "strow_1",
            "cells": [
                {
                    "inputValue": {
                        "__typename": "ObjectLinkValue",
                        "value": {"__typename": "Container", "id": "con_abc123"},
                    }
                },
                {
                    "inputValue": {
                        "__typename": "ObjectLinkListValue",
                        "value": [
                            {"__typename": "Container", "id": "con_abc123"},
                            {"__typename": "Container", "id": "con_def456"},
                        ],
                    }
                },
                {"inputValue": {"__typename": "TextValue", "value": ""}},
            ],
        }
    ]
    fake.parts.append(
        _results_part("docpart_links", name="Assay results", schema_id="assaysch_abc123")
    )
    json_path = tmp_path / "links.json"
    summary = await _export(
        runtime,
        json_path,
        kind="results-table",
        file_format="json",
        part_id="strtbl_example",
        entry=ENTRY_ID,
    )
    assert summary["row_count"] == 1
    assert summary["columns"] == ["Tube", "Contents", "Quantity"]
    assert summary["source"]["part_id"] == "docpart_links"
    assert summary["source"]["table_id"] == "strtbl_example"
    document = json.loads(json_path.read_text(encoding="utf-8"))
    assert document["rows"] == [
        {
            "Tube": "con_abc123",
            "Contents": ["con_abc123", "con_def456"],
            "Quantity": "",
        }
    ]
    assert "Container" not in json_path.read_text(encoding="utf-8")
    assert "con_abc123" not in json.dumps(summary)
    csv_path = tmp_path / "links.csv"
    csv_summary = await _export(
        runtime,
        csv_path,
        kind="results-table",
        file_format="csv",
        part_id="docpart_links",
    )
    assert list(csv.reader(csv_path.read_text(encoding="utf-8").splitlines())) == [
        ["Tube", "Contents", "Quantity"],
        ["con_abc123", '["con_abc123","con_def456"]', ""],
    ]
    assert csv_summary["checksum"] == hashlib.sha256(csv_path.read_bytes()).hexdigest()
    assert fake.writes() == []


async def test_assay_export_keeps_values_when_the_schema_cannot_be_read(
    runtime, fake, tmp_path
) -> None:
    fake.results = [
        {
            "id": "result_1",
            "createdAt": "2026-01-01T00:00:00Z",
            "schemaId": "assaysch_none",
            "schemaFields": [
                {
                    "fieldDefinition": {"id": "assayfield_orphan", "systemName": "orphan"},
                    "value": _text("kept-value"),
                }
            ],
        }
    ]
    path = tmp_path / "orphan.csv"
    summary = await _export(
        runtime,
        path,
        kind="assay-results",
        file_format="csv",
        schema_id="assaysch_none",
    )
    assert summary["columns"] == ["id", "schemaId", "createdAt", "orphan"]
    assert "kept-value" not in json.dumps(summary)
    assert "kept-value" in path.read_text(encoding="utf-8")
    assert summary["row_count"] == 1


async def test_listed_results_table_uses_the_only_results_part(runtime, fake, tmp_path) -> None:
    fake.result_table_schema = None
    fake.add_results_table("docpart_only")
    fake.parts.append(_results_part("docpart_only", name="Only plate", schema_id=None))
    summary = await _export(
        runtime,
        tmp_path / "only.csv",
        kind="results-table",
        file_format="csv",
        part_id="strtbl_example",
        entry=ENTRY_ID,
    )
    assert summary["row_count"] == 3
    assert summary["source"]["part_id"] == "docpart_only"
    assert summary["source"]["table_id"] == "strtbl_example"
    assert not any(call.path.endswith("/result/items") for call in fake.calls)


async def test_assay_export_uses_a_display_name_on_the_field_definition(
    runtime, fake, tmp_path
) -> None:
    fake.results = [
        {
            "id": "result_1",
            "__typename": "Result",
            "createdAt": "2026-01-01T00:00:00Z",
            "archived": False,
            "schemaFields": [
                {
                    "fieldDefinition": {
                        "id": "assayfield_tube",
                        "name": "Tube",
                        "systemName": "tube",
                    },
                    "value": _text("tube-a"),
                }
            ],
        }
    ]
    summary = await _export(
        runtime, tmp_path / "named.csv", kind="assay-results", file_format="csv"
    )
    assert summary["columns"] == ["id", "createdAt", "Tube", "__typename", "archived"]
    assert "tube" not in summary["columns"]
    assert (
        list(csv.reader((tmp_path / "named.csv").read_text(encoding="utf-8").splitlines()))[1][2]
        == "tube-a"
    )
    assert not any("/field-definitions/items" in call.path for call in fake.calls)


def _results_part(
    part_id: str, *, name: str, schema_id: str | None, api_id: str | None = None
) -> dict[str, Any]:
    part: dict[str, Any] = {
        "id": part_id,
        "__typename": "DocumentPartResultsTable",
        "name": name,
    }
    if schema_id is not None:
        part["resultSchema"] = {"__typename": "ResultSchemaRef", "id": schema_id}
    if api_id is not None:
        part["apiId"] = api_id
    return part


async def test_listed_results_table_resolves_to_the_document_part(runtime, fake, tmp_path) -> None:
    fake.add_results_table("docpart_rt1")
    fake.results_row_page_size = 1
    fake.parts.append(_results_part("docpart_rt1", name="Plate", schema_id="assaysch_abc123"))
    path = tmp_path / "listed.csv"
    summary = await _export(
        runtime,
        path,
        kind="results-table",
        file_format="csv",
        part_id="strtbl_example",
        entry=ENTRY_ID,
    )
    assert summary["row_count"] == 3
    assert summary["columns"] == ["Ct"]
    assert summary["source"]["part_id"] == "docpart_rt1"
    assert summary["source"]["table_id"] == "strtbl_example"
    assert summary["source"]["id_namespace"] == "v3_document_part"
    assert summary["source"]["listed_id_namespace"] == "v2_structured_table"
    assert summary["source"]["entry_id"] == ENTRY_ID
    assert path.read_text(encoding="utf-8").splitlines()[1] == "12"
    assert not any(
        call.path.endswith("/document-part-results-table/strtbl_example") for call in fake.calls
    )
    assert any(
        call.path.endswith("/document-part-results-table/docpart_rt1") for call in fake.calls
    )
    assert [call.params.get("nextToken") for call in _row_calls(fake)] == [None, "1", "2"]
    assert fake.writes() == []


async def test_listed_results_table_prefers_a_handle_and_a_shared_name(
    runtime, fake, tmp_path
) -> None:
    fake.add_results_table("docpart_handle")
    fake.parts.append(
        _results_part(
            "docpart_handle",
            name="Other",
            schema_id="assaysch_two",
            api_id="strtbl_example",
        )
    )
    fake.parts.append(
        _results_part("docpart_other", name="Assay results", schema_id="assaysch_abc123")
    )
    summary = await _export(
        runtime,
        tmp_path / "handle.csv",
        kind="results-table",
        file_format="csv",
        part_id="strtbl_example",
        entry="EXP-1",
    )
    assert summary["source"]["part_id"] == "docpart_handle"
    assert any(
        call.path.endswith("/document-part-results-table/docpart_handle") for call in fake.calls
    )
    assert not any("docpart_other" in call.path for call in fake.calls)

    fake.calls.clear()
    fake.results_parts.clear()
    fake.parts = [
        part for part in fake.parts if part.get("id") not in {"docpart_handle", "docpart_other"}
    ]
    fake.add_results_table("docpart_named")
    fake.parts.append(
        _results_part("docpart_named", name="Assay results", schema_id="assaysch_abc123")
    )
    fake.parts.append(
        _results_part("docpart_extra", name="Other plate", schema_id="assaysch_abc123")
    )
    named = await _export(
        runtime,
        tmp_path / "named.csv",
        kind="results-table",
        file_format="csv",
        part_id="strtbl_example",
        entry=ENTRY_ID,
    )
    assert named["source"]["part_id"] == "docpart_named"


async def test_listed_results_table_falls_back_to_assay_results(runtime, fake, tmp_path) -> None:
    _install_result_fields(fake)
    fake.results = [
        _v3_result(
            "result_1",
            [
                _schema_field("assayfield_tube", _link("ObjectLinkValue", "con_abc123")),
                _schema_field("assayfield_contents", _text("lysis buffer")),
                _schema_field("assayfield_registry", _text("REG-1")),
                _schema_field("assayfield_entry", None, linked=ENTRY_ID),
                _schema_field(
                    "assayfield_quantity", {"__typename": "DecimalValue", "value": "2.5"}
                ),
                _schema_field("assayfield_unit", _text("mL")),
            ],
        )
    ]
    fake.parts.append(_results_part("docpart_a", name="Plate A", schema_id="assaysch_abc123"))
    fake.parts.append(_results_part("docpart_b", name="Plate B", schema_id="assaysch_abc123"))
    path = tmp_path / "fallback.csv"
    summary = await _export(
        runtime,
        path,
        kind="results-table",
        file_format="csv",
        part_id="strtbl_example",
        entry=ENTRY_ID,
    )
    assert summary["source"]["part_id"] == "strtbl_example"
    assert summary["source"]["id_namespace"] == "v2_structured_table"
    assert summary["source"]["export_via"] == "assay_results"
    assert summary["source"]["entry_id"] == ENTRY_ID
    assert summary["source"]["schema_id"] == "assaysch_abc123"
    assert "Tube" in summary["columns"]
    assert "con_abc123" not in json.dumps(summary)
    assert path.read_text(encoding="utf-8").splitlines()[1].split(",")[3] == "con_abc123"
    result_calls = [call for call in fake.calls if call.path.endswith("/result/items")]
    assert result_calls[0].params["entry.eq"] == ENTRY_ID
    assert result_calls[0].params["schema.eq"] == "assaysch_abc123"
    assert not any("document-part-results-table/strtbl_example" in call.path for call in fake.calls)
    assert not any(
        call.path.endswith("/document-part-results-table/docpart_a") for call in fake.calls
    )
    assert fake.writes() == []


async def test_listed_table_id_without_a_resolution_is_a_validation_error(
    runtime, fake, tmp_path
) -> None:
    missing = tmp_path / "missing.csv"
    with pytest.raises(ValidationError, match="v3_document_part") as caught:
        await _export(
            runtime,
            missing,
            kind="results-table",
            file_format="csv",
            part_id="strtbl_example",
        )
    assert caught.value.details["id_namespace"] == "v2_structured_table"
    assert caught.value.details["expected_id_namespace"] == "v3_document_part"
    assert "results tables" in str(caught.value)
    assert fake.calls == []
    assert not missing.exists()

    with pytest.raises(ValidationError, match="v3_document_part") as table_kind:
        await _export(
            runtime,
            tmp_path / "table.csv",
            kind="table",
            file_format="csv",
            part_id="strtbl_example",
            entry=ENTRY_ID,
        )
    assert table_kind.value.details["expected_id_namespace"] == "v3_document_part"
    assert "results-table" in str(table_kind.value)
    assert not any(
        "document-part-unstructured-table/strtbl_example" in call.path for call in fake.calls
    )
    assert not (tmp_path / "table.csv").exists()

    with pytest.raises(ValidationError, match="not found") as absent:
        await _export(
            runtime,
            tmp_path / "absent.csv",
            kind="results-table",
            file_format="csv",
            part_id="strtbl_missing",
            entry=ENTRY_ID,
        )
    assert absent.value.details["id_namespace"] == "v2_structured_table"
    assert absent.value.details["entry_id"] == ENTRY_ID
    assert not any(
        "strtbl_missing" in call.path and "document-part" in call.path for call in fake.calls
    )

    fake.result_table_schema = None
    with pytest.raises(ValidationError, match="no result schema"):
        await _export(
            runtime,
            tmp_path / "noschema.csv",
            kind="results-table",
            file_format="csv",
            part_id="strtbl_example",
            entry=ENTRY_ID,
        )
    assert not any(call.path.endswith("/result/items") for call in fake.calls)


async def test_listed_unstructured_table_uses_a_part_that_carries_the_handle(
    runtime, fake, tmp_path
) -> None:
    table = next(part for part in fake.parts if part["id"] == TABLE_PART)
    table["apiId"] = "strtbl_example"
    summary = await _export(
        runtime,
        tmp_path / "handled.csv",
        kind="table",
        file_format="csv",
        part_id="strtbl_example",
        entry=ENTRY_ID,
    )
    assert summary["row_count"] == 3
    assert summary["source"]["part_id"] == TABLE_PART
    assert summary["source"]["table_id"] == "strtbl_example"
    assert summary["source"]["id_namespace"] == "v3_document_part"
    assert any(
        call.path.endswith(f"/document-part-unstructured-table/{TABLE_PART}") for call in fake.calls
    )
    assert not any(
        "strtbl_example" in call.path and "document-part" in call.path for call in fake.calls
    )
    assert fake.writes() == []


async def test_listed_table_export_still_refuses_to_overwrite(runtime, fake, tmp_path) -> None:
    path = tmp_path / "kept.csv"
    path.write_text("keep-me\n", encoding="utf-8")
    with pytest.raises(ValidationError, match="overwrite"):
        await _export(
            runtime,
            path,
            kind="results-table",
            file_format="csv",
            part_id="strtbl_example",
            entry=ENTRY_ID,
        )
    assert path.read_text(encoding="utf-8") == "keep-me\n"
    assert fake.calls == []


async def test_assay_export_stops_on_a_repeated_token(runtime, fake, tmp_path) -> None:
    fake.repeat_next_token = True
    path = tmp_path / "loop.json"
    with pytest.raises(UpstreamError, match="repeated pagination token"):
        await _export(runtime, path, kind="assay-results", file_format="json")
    assert not path.exists()
    assert _partials(tmp_path) == []


async def test_export_does_not_write_a_file_when_the_read_fails(runtime, tmp_path) -> None:
    path = tmp_path / "missing.csv"
    with pytest.raises(NotFoundError):
        await _export(runtime, path, kind="table", file_format="csv", part_id="docpart_missing")
    assert not path.exists()


async def test_export_refuses_to_overwrite_without_the_flag(runtime, fake, tmp_path) -> None:
    path = tmp_path / "kept.csv"
    path.write_text("keep-me\n", encoding="utf-8")
    with pytest.raises(ValidationError, match="overwrite"):
        await _export(runtime, path, kind="table", file_format="csv", part_id=TABLE_PART)
    assert path.read_text(encoding="utf-8") == "keep-me\n"
    assert fake.calls == []

    summary = await _export(
        runtime,
        path,
        kind="table",
        file_format="csv",
        part_id=TABLE_PART,
        overwrite=True,
    )
    assert summary["row_count"] == 3
    assert path.read_text(encoding="utf-8").splitlines()[0] == "Sample,Value"
    assert "keep-me" not in path.read_text(encoding="utf-8")
    assert fake.writes() == []


@pytest.mark.parametrize(
    ("kwargs", "match"),
    [
        ({"kind": None, "file_format": "json", "part_id": TABLE_PART}, "kind must be"),
        ({"kind": "nope", "file_format": "json", "part_id": TABLE_PART}, "kind must be"),
        ({"kind": "table", "file_format": None, "part_id": TABLE_PART}, "format must be"),
        ({"kind": "table", "file_format": "xml", "part_id": TABLE_PART}, "format must be"),
        ({"kind": "table", "file_format": "csv"}, "require part_id"),
        ({"kind": "table", "file_format": "csv", "part_id": "  "}, "part_id must be"),
        ({"kind": "table", "file_format": "csv", "part_id": 1}, "part_id must be"),
        (
            {"kind": "table", "file_format": "csv", "part_id": TABLE_PART, "entry": ENTRY_ID},
            "assay-results",
        ),
        (
            {
                "kind": "results-table",
                "file_format": "csv",
                "part_id": "docpart_rt1",
                "schema_id": "assaysch_abc123",
            },
            "assay-results",
        ),
        (
            {"kind": "assay-results", "file_format": "csv", "part_id": TABLE_PART},
            "part_id applies only",
        ),
        (
            {"kind": "table", "file_format": "csv", "part_id": TABLE_PART, "overwrite": 1},
            "overwrite must be",
        ),
        ({"kind": "table", "file_format": "csv", "part_id": TABLE_PART, "path": "  "}, "path must"),
        ({"kind": "table", "file_format": "csv", "part_id": TABLE_PART, "path": None}, "path must"),
        (
            {"kind": "assay-results", "file_format": "csv", "entry": "  "},
            "entry must be",
        ),
        (
            {"kind": "assay-results", "file_format": "csv", "schema_id": ""},
            "schema_id must be",
        ),
    ],
)
async def test_export_rejects_bad_arguments_before_any_request(
    runtime, fake, tmp_path, kwargs, match
) -> None:
    path = kwargs.pop("path", str(tmp_path / "out.csv"))
    with pytest.raises(ValidationError, match=match):
        await export_dataset(runtime.service, path=path, **kwargs)
    assert fake.calls == []


async def test_export_rejects_a_directory_and_a_missing_parent(runtime, fake, tmp_path) -> None:
    with pytest.raises(ValidationError, match="not a file"):
        await _export(runtime, tmp_path, kind="table", file_format="csv", part_id=TABLE_PART)
    missing = tmp_path / "missing" / "out.csv"
    with pytest.raises(ValidationError, match="directory does not exist"):
        await _export(runtime, missing, kind="table", file_format="csv", part_id=TABLE_PART)
    assert fake.calls == []


async def test_export_write_errors_leave_no_file(runtime, fake, tmp_path, monkeypatch) -> None:
    path = tmp_path / "new.csv"

    def fail_write(self, data: bytes) -> int:
        raise OSError("read-only file system")

    monkeypatch.setattr(Path, "write_bytes", fail_write)
    with pytest.raises(ValidationError, match="Could not write") as caught:
        await _export(runtime, path, kind="table", file_format="csv", part_id=TABLE_PART)
    assert caught.value.details["reason"] == "read-only file system"
    assert not path.exists()
    assert _partials(tmp_path) == []
    assert fake.writes() == []
    monkeypatch.undo()

    kept = tmp_path / "kept.csv"
    kept.write_text("still-here\n", encoding="utf-8")

    def clash(src: str, dst: str) -> None:
        raise FileExistsError(dst)

    monkeypatch.setattr(os, "link", clash)
    fresh = tmp_path / "race.csv"
    with pytest.raises(ValidationError, match="overwrite"):
        await _export(runtime, fresh, kind="table", file_format="csv", part_id=TABLE_PART)
    assert not fresh.exists()
    assert kept.read_text(encoding="utf-8") == "still-here\n"
    assert _partials(tmp_path) == []


def test_cell_and_field_helpers_cover_empty_values() -> None:
    assert _cell_value({"evaluationValue": {"value": None}, "inputValue": {"value": "kept"}}) == (
        "kept"
    )
    assert _cell_value({}) is None
    assert _cell_value({"inputValue": {"value": ""}}) == ""
    assert (
        _cell_value(
            {
                "evaluationValue": {
                    "value": {"id": "con_eval", "__typename": "Container"},
                },
                "inputValue": {"value": "typed"},
            }
        )
        == "con_eval"
    )
    assert _cell_value({"inputValue": {"value": False}}) is False
    assert _cell_value({"evaluationValue": {"value": 0}}) == 0
    assert _table_row(
        {
            "cells": [
                {
                    "inputValue": {
                        "value": {"id": "con_abc123", "__typename": "Container"},
                    }
                },
                {
                    "inputValue": {
                        "value": [
                            {"id": "con_abc123", "__typename": "Container"},
                            {"id": "con_def456", "type": "entity"},
                        ]
                    }
                },
                {"inputValue": {"value": ""}},
            ]
        },
        ["Tube", "Contents", "Quantity"],
    ) == {
        "Tube": "con_abc123",
        "Contents": ["con_abc123", "con_def456"],
        "Quantity": "",
    }
    linked = {
        "id": "result_1",
        "schemaFields": [
            {
                "fieldDefinition": {
                    "id": "assayfield_text",
                    "name": "Note",
                    "__typename": "TextFieldDefinition",
                },
                "value": None,
                "linkedEntityId": "bfi_other",
            },
            {
                "fieldDefinition": {
                    "id": "assayfield_link",
                    "name": "Sample",
                    "__typename": "EntryLinkFieldDefinition",
                },
                "value": None,
                "linkedEntityId": "etr_other",
            },
            {
                "fieldDefinition": {
                    "id": "assayfield_forced",
                    "name": "Forced",
                    "__typename": "EntryLinkFieldDefinition",
                },
                "value": None,
                "linkedEntityId": "etr_forced",
            },
            {
                "fieldDefinition": "assayfield_unknown",
                "value": None,
                "linkedEntityId": "seq_abc123",
            },
            {
                "fieldDefinition": {
                    "id": "assayfield_decimal",
                    "name": "Amount",
                    "__typename": "DecimalFieldDefinition",
                },
                "value": None,
                "linkedEntityId": "bfi_amount",
            },
            {
                "fieldDefinition": {"type": "entity_link", "name": "Custom"},
                "value": None,
                "linkedEntityId": "ent_abc123",
            },
            {
                "fieldDefinition": {"name": "Bare"},
                "linkedEntityId": "ent_bare",
            },
            {
                "fieldDefinition": {
                    "__typename": "SchemaFieldDefinitionRef",
                    "id": "assayfield_ref",
                },
                "value": None,
                "linkedEntityId": "ent_ref",
            },
            {
                "fieldDefinition": {
                    "id": "assayfield_flag",
                    "name": "Flag",
                    "__typename": "BooleanFieldDefinition",
                },
                "value": False,
                "linkedEntityId": "result_1",
            },
        ],
    }
    flags = {"assayfield_text": False, "assayfield_unknown": True, "assayfield_forced": False}
    linked_row = _assay_row(
        linked,
        [
            "id",
            "Note",
            "Sample",
            "Forced",
            "assayfield_unknown",
            "Amount",
            "Custom",
            "Bare",
            "assayfield_ref",
            "Flag",
        ],
        {},
        flags,
    )
    assert linked_row["Note"] is None
    assert linked_row["Flag"] is False
    assert linked_row["Sample"] == "etr_other"
    assert linked_row["Forced"] is None
    assert linked_row["assayfield_unknown"] == "seq_abc123"
    assert linked_row["Amount"] is None
    assert linked_row["Custom"] == "ent_abc123"
    assert linked_row["Bare"] == "ent_bare"
    assert linked_row["assayfield_ref"] == "ent_ref"
    loose = _assay_row(
        {
            "schemaFields": [
                {
                    "fieldDefinition": {"name": "Loose"},
                    "value": None,
                    "linkedEntityId": "ent_loose",
                }
            ]
        },
        ["Loose"],
    )
    assert loose["Loose"] == "ent_loose"
    assert _assay_columns([{"fields": {1: {"value": "nope"}, "ct": {"value": 1}}}]) == ["ct"]
    records = [
        {
            "id": "result_1",
            "schemaId": "assaysch_abc123",
            "schemaFields": [
                "skip",
                {"fieldDefinition": "assayfield_tube", "value": {"textValue": "from-text"}},
                {"fieldDefinition": {"displayName": "Shown"}, "value": {"displayValue": "shown"}},
                {
                    "fieldDefinition": {"id": "assayfield_missing", "systemName": "tube"},
                    "value": {"type": "entity", "id": "seq_abc123"},
                },
                {"fieldDefinition": {"id": "assayfield_bare"}, "value": _text("bare-value")},
                {"fieldDefinition": {"id": "assayfield_tube"}, "value": "second-tube"},
                {"fieldDefinition": {"name": "Empty"}},
            ],
            "fields": {"": {"value": "skip"}, "extra": {"value": "kept-extra"}},
        },
        {"schema": {"id": ""}, "schemaFields": {"Contents": {"value": "map-value"}}},
    ]
    columns = _assay_columns(records, {"assayfield_tube": "Tube"})
    assert columns == [
        "id",
        "schemaId",
        "Tube",
        "Shown",
        "tube",
        "assayfield_bare",
        "Empty",
        "extra",
        "Contents",
    ]
    row = _assay_row(records[0], columns, {"assayfield_tube": "Tube"})
    assert row["Tube"] == "from-text"
    assert row["Shown"] == "shown"
    assert row["tube"] == "seq_abc123"
    assert row["assayfield_bare"] == "bare-value"
    assert row["Empty"] is None
    assert row["extra"] == "kept-extra"
    mapped = _assay_row(records[1], columns, {})
    assert mapped["Contents"] == "map-value"
    assert mapped["Tube"] is None


@pytest.fixture
def invoke(benchling_env, fake, monkeypatch):
    monkeypatch.setattr(cli, "Runtime", lambda environment: runtime_for(fake, environment))

    def run(*args: str, env: str | None = "sandbox"):
        suffix = ["--env", env] if env else []
        result = CliRunner().invoke(cli.app, [*args, *suffix])
        payload = json.loads(result.stdout) if result.stdout.strip() else {}
        return result.exit_code, payload, result

    return run


def test_cli_export_uses_the_envelope_and_exit_codes(invoke, fake, tmp_path) -> None:
    path = tmp_path / "table.csv"
    code, payload, _ = invoke(
        "export",
        "--kind",
        "table",
        "--part",
        TABLE_PART,
        "--format",
        "CSV",
        "--output",
        str(path),
    )
    assert code == 0
    assert payload["ok"] is True and payload["error"] is None
    assert payload["schema_version"] == "1.0"
    assert payload["meta"]["env"] == "sandbox"
    assert payload["meta"]["tenant_url"] == TENANT
    assert payload["data"]["row_count"] == 3
    assert payload["data"]["checksum"] == _sha256(path)
    assert "=SUM(A1:A2)" not in json.dumps(payload)
    assert fake.writes() == []

    code, refused, _ = invoke(
        "export",
        "--kind",
        "table",
        "--part",
        TABLE_PART,
        "--format",
        "csv",
        "--output",
        str(path),
    )
    assert code == 2
    assert refused["ok"] is False and refused["data"] is None
    assert refused["error"]["code"] == "validation_error"
    assert path.read_text(encoding="utf-8").splitlines()[1] == "A,1"

    missing = tmp_path / "missing.csv"
    code, not_found, _ = invoke(
        "export",
        "--kind",
        "table",
        "--part",
        "docpart_missing",
        "--format",
        "csv",
        "--output",
        str(missing),
    )
    assert code == 4
    assert not_found["error"]["code"] == "not_found"
    assert not missing.exists()

    code, validation, _ = invoke(
        "export",
        "--kind",
        "table",
        "--part",
        TABLE_PART,
        "--format",
        "csv",
        "--output",
        str(tmp_path / "no-env.csv"),
        env=None,
    )
    assert code == 2
    assert validation["error"]["code"] == "validation_error"


def test_cli_export_rejects_a_listed_table_id_without_entry(invoke, fake, tmp_path) -> None:
    path = tmp_path / "listed.csv"
    code, payload, _ = invoke(
        "export",
        "--kind",
        "results-table",
        "--part",
        "strtbl_example",
        "--format",
        "csv",
        "--output",
        str(path),
    )
    assert code == 2
    assert payload["error"]["code"] == "validation_error"
    assert payload["error"]["details"]["id_namespace"] == "v2_structured_table"
    assert payload["error"]["details"]["expected_id_namespace"] == "v3_document_part"
    assert "results tables" in payload["error"]["message"]
    assert "entry inspect" in payload["error"]["message"]
    assert not path.exists()
    assert fake.calls == []


def test_cli_export_help_names_the_part_id_namespaces() -> None:
    result = CliRunner().invoke(cli.app, ["export", "--help"])
    assert result.exit_code == 0
    assert "v3_document_part" in result.stdout
    assert "v2_structured_table" in result.stdout
    assert "entry inspect" in result.stdout
    assert "results tables" in result.stdout


class _Session:
    def __init__(self, server) -> None:
        self.server = server

    async def call_tool(self, name, arguments):
        async with Client(self.server._mcp_server) as active:
            return await active.call_tool(name, arguments)


async def test_mcp_export_returns_a_summary_and_does_not_write_remotely(
    pool, fake, tmp_path
) -> None:
    session = _Session(build_mcp_server(pool))
    path = tmp_path / "assay.json"
    result = await session.call_tool(
        "export",
        {
            "env": "sandbox",
            "kind": "assay-results",
            "output": str(path),
            "format": "json",
            "entry": ENTRY_ID,
        },
    )
    assert result.isError is False
    payload = result.structuredContent
    assert payload["ok"] is True
    assert payload["meta"]["env"] == "sandbox"
    assert payload["data"]["row_count"] == 3
    assert payload["data"]["columns"] == ["id", "entryId", "schemaId"]
    assert "result_1" not in json.dumps(payload["data"])
    assert json.loads(path.read_text(encoding="utf-8"))["rows"][0]["id"] == "result_1"
    assert fake.writes() == []

    blocked = await session.call_tool(
        "export",
        {"env": "sandbox", "kind": "nope", "output": str(tmp_path / "no.json"), "format": "json"},
    )
    assert blocked.isError is False
    assert blocked.structuredContent["error"]["code"] == "validation_error"
    assert not (tmp_path / "no.json").exists()

    existing = await session.call_tool(
        "export",
        {
            "env": "sandbox",
            "kind": "assay-results",
            "output": str(path),
            "format": "json",
        },
    )
    assert existing.structuredContent["error"]["code"] == "validation_error"
    assert json.loads(path.read_text(encoding="utf-8"))["rows"][0]["id"] == "result_1"
