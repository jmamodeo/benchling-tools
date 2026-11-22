"""Typed change specifications keep every supported form and reject the rest."""

from __future__ import annotations

import json

import pytest

from benchling_tools.change_spec import CHANGE_SPECIFICATION_SCHEMA, parse_change_specification
from benchling_tools.errors import ValidationError

TEXT = "docpart_text1"


def parse(value):
    return parse_change_specification(value)


def test_schema_inlines_every_supported_shape() -> None:
    encoded = json.dumps(CHANGE_SPECIFICATION_SCHEMA)
    assert "$ref" not in encoded
    assert len(CHANGE_SPECIFICATION_SCHEMA["anyOf"]) == 13
    for token in (
        "entry_template_id",
        "folder_id",
        "schema_fields",
        "insert",
        "delete",
        "move",
        "replace_text",
        "set_cells",
        "append_rows",
        "schema_fields",
        "table_id",
        "assay_results",
        "batches",
        "csv_path",
        "id_column",
        "column_map",
        "update",
        "schema_id",
        "source_container_id",
        "destination_container_id",
        "destination_contents",
    ):
        assert token in encoded


def test_entry_creation_specification_round_trips_optional_schema_fields() -> None:
    request = {
        "entry_template_id": "etpl_abc123",
        "folder_id": "lib_abc123",
        "name": "Fresh experiment",
        "schema_fields": {"temperature": {"value": 21}},
    }

    assert parse_change_specification(request) == request
    with pytest.raises(ValidationError):
        parse_change_specification({**request, "extra": True})


def test_document_table_and_field_forms_round_trip() -> None:
    assert parse({"operation": "delete", "from_part_id": TEXT}) == {
        "operation": "delete",
        "from_part_id": TEXT,
    }
    assert parse(
        {"operation": "insert", "parts": [{"contentType": "text", "text": "tail", "links": []}]}
    ) == {
        "operation": "insert",
        "parts": [{"contentType": "text", "text": "tail", "links": []}],
    }
    moved = parse(
        {
            "operation": "move",
            "from_part_id": TEXT,
            "to_part_id": "docpart_text2",
            "position": "end_of_document",
        }
    )
    assert moved["to_part_id"] == "docpart_text2"
    replaced = parse({"operation": "replace_text", "part_id": TEXT, "text": "new"})
    assert replaced == {"operation": "replace_text", "part_id": TEXT, "text": "new"}
    kept = parse(
        {
            "operation": "replace_text",
            "part_id": TEXT,
            "text": "new",
            "indentation": 0,
            "formats": [],
        }
    )
    assert kept["indentation"] == 0
    assert kept["formats"] == []
    cells = parse(
        {
            "operation": "set_cells",
            "table_part_id": "docpart_table1",
            "updates": [{"row": 0, "column": 1, "value": "1.25"}],
        }
    )
    assert cells["updates"] == [{"row": 0, "column": 1, "value": "1.25"}]
    rows = parse(
        {
            "operation": "append_rows",
            "table_part_id": "docpart_table1",
            "rows": [["D", "4"], {"cells": ["F", 6]}],
        }
    )
    assert rows["rows"] == [["D", "4"], {"cells": ["F", 6]}]
    fields = parse({"schema_fields": {"notes": {"value": None}}})
    assert fields == {"schema_fields": {"notes": {"value": None}}}


def test_inline_records_and_csv_updates_round_trip() -> None:
    uploaded = parse(
        {
            "table_id": "strtbl_example",
            "assay_results": [
                {
                    "schemaId": "assaysch_abc123",
                    "projectId": None,
                    "fields": {"ct": {"value": False}},
                }
            ],
        }
    )
    record = uploaded["assay_results"][0]
    assert record["projectId"] is None
    assert record["fields"] == {"ct": {"value": False}}
    assert "id" not in record
    multi_table = parse(
        {
            "batches": [
                {
                    "table_id": "strtbl_example",
                    "assay_results": [
                        {"schemaId": "assaysch_abc123", "fields": {"ct": {"value": 12}}}
                    ],
                },
                {
                    "table_id": "strtbl_other",
                    "assay_results": [
                        {"schemaId": "assaysch_other", "fields": {"ct": {"value": 8}}}
                    ],
                },
            ]
        }
    )
    assert len(multi_table["batches"]) == 2
    updated = parse(
        {
            "operation": "update",
            "csv_path": "assay.csv",
            "id_column": "id",
            "column_map": {"Ct": "Ct"},
        }
    )
    assert updated["column_map"] == {"Ct": "Ct"}
    # CSV keys alone still select the update form. operation is then required.
    with pytest.raises(ValidationError, match="operation"):
        parse({"csv_path": "assay.csv", "id_column": "id", "column_map": {"Ct": "Ct"}})


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("nope", "must be an object"),
        (["delete"], "must be an object"),
        (None, "must be an object"),
        ({"operation": "explode"}, "supported shape"),
        ({"operation": "delete", "from_part_id": 1}, "from_part_id"),
        ({"operation": "delete", "from_part_id": TEXT, "extra": True}, "extra"),
        ({"operation": "delete", "from_part_id": TEXT, "to_part_id": None}, "to_part_id"),
        ({"operation": "set_cells", "table_part_id": "strtbl_example", "updates": []}, "docpart_"),
        (
            {
                "operation": "set_cells",
                "table_part_id": "docpart_table1",
                "updates": [{"row": True, "column": 0, "value": "x"}],
            },
            "updates.0.row",
        ),
        ({"schema_fields": {}}, "schema_fields"),
        ({"schema_fields": {"notes": {"text": "x"}}}, "text"),
        ({"schema_fields": {"notes": {"value": "x"}}, "table_id": "strtbl_example"}, "mixes"),
        (
            {
                "table_id": "strtbl_example",
                "assay_results": [
                    {"id": "result_mine", "schemaId": "assaysch_abc123", "fields": {}}
                ],
            },
            "omit id",
        ),
        (
            {
                "operation": "update",
                "csv_path": "assay.csv",
                "id_column": "id",
                "column_map": {" ": "Ct"},
            },
            "column names",
        ),
        (
            {
                "operation": "update",
                "id_column": "id",
                "column_map": {"Ct": "Ct"},
            },
            "csv_path",
        ),
    ],
)
def test_invalid_specifications_name_the_problem(value, expected) -> None:
    with pytest.raises(ValidationError, match=expected) as caught:
        parse(value)
    assert caught.value.code == "validation_error"
    encoded = json.dumps(caught.value.details)
    assert "result_mine" not in encoded
