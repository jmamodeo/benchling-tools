"""CSV column mapping and PATCH /result updates."""

from __future__ import annotations

import csv
import io
from pathlib import Path
from typing import Any

import pytest
from fake_benchling import ENTRY_ID

from benchling_tools.errors import ConflictError, PartialCompletionError, ValidationError
from benchling_tools.export import export_dataset
from benchling_tools.plans.result_update import _parse_cell

MODIFIED = "2026-01-02T00:00:00Z"


def test_decimal_csv_cell_is_parsed_as_a_number():
    definition = {"__typename": "DecimalFieldDefinition"}
    assert _parse_cell(definition, "2.5", column="Quantity") == 2.5
    with pytest.raises(ValidationError, match="must be a number"):
        _parse_cell(definition, "bad", column="Quantity")


def _field(field_id: str, value: Any) -> dict[str, Any]:
    return {
        "__typename": "SchemaFieldValue",
        "fieldDefinition": {"id": field_id, "__typename": "SchemaFieldDefinitionRef"},
        "value": value,
    }


def _text(value: str) -> dict[str, Any]:
    return {"__typename": "TextValue", "value": value}


def _integer(value: int) -> dict[str, Any]:
    return {"__typename": "IntegerValue", "value": value}


def make_result(
    result_id: str = "result_1",
    *,
    ct: Any = 12,
    note: Any = "n",
    modified: str | None = MODIFIED,
    entry: str | None = ENTRY_ID,
    schema: str | None = "assaysch_abc123",
    extra: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """A V3 result on the synthetic entry, with Ct and Note schema fields."""
    record: dict[str, Any] = {
        "id": result_id,
        "createdAt": "2026-01-01T00:00:00Z",
        "schemaFields": [
            _field("assayfield_ct", None if ct is None else _integer(ct)),
            _field("assayfield_note", None if note is None else _text(note)),
            *(extra or []),
        ],
    }
    if modified is not None:
        record["modifiedAt"] = modified
    if entry is not None:
        record["entry"] = {"id": entry, "__typename": "Entry"}
    if schema is not None:
        record["schema"] = {"id": schema, "__typename": "ResultSchemaRef"}
    return record


def write_csv(path: Path, headers: list[str], rows: list[list[Any]]) -> Path:
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(headers)
    writer.writerows(rows)
    path.write_text(buffer.getvalue(), encoding="utf-8")
    return path


def _request(path: Path, column_map: dict[str, str], id_column: str = "id") -> dict[str, Any]:
    return {
        "operation": "update",
        "csv_path": str(path),
        "id_column": id_column,
        "column_map": column_map,
    }


async def plan_csv(runtime, path: Path, column_map: dict[str, str], **kwargs: Any):
    return await runtime.planner.plan(ENTRY_ID, "results", _request(path, column_map, **kwargs))


def _no_result_reads(fake) -> bool:
    return not any(call.path.startswith("/api/v3/result/") for call in fake.calls)


@pytest.mark.parametrize("field", ["Ct", "ct", "assayfield_ct"])
async def test_csv_update_resolves_columns_and_patches_by_field_id(
    runtime, fake, tmp_path, field
) -> None:
    fake.results = [make_result()]
    path = write_csv(
        tmp_path / "assay.csv",
        ["id", "createdAt", "Ct", "Note"],
        [["result_1", "2026-01-01T00:00:00Z", "15", "n"]],
    )
    planned = await plan_csv(runtime, path, {"Ct": field, "Note": "Note"})
    assert fake.writes() == []
    preview = planned.after["results"][0]
    assert preview["schema_fields"] == {"assayfield_ct": {"value": 15}}
    assert preview["fields"]["ct"]["value"] == 15
    assert preview["fields"]["ct"]["field_definition_id"] == "assayfield_ct"
    assert preview["fields"]["ct"]["name"] == "Ct"
    assert planned.before["results"][0]["fields"]["ct"]["value"] == 12
    assert planned.before["results"][0]["modified_at"] == MODIFIED
    assert planned.diff["changed"] is True
    assert planned.preconditions["operation"] == "update"
    assert planned.preconditions["results"][0]["modified_at"] == MODIFIED
    assert "modifiedAt" in planned.guarantees[0]
    assert any("field-definition" in item for item in planned.guarantees)
    assert any("createdAt" in item for item in planned.warnings)
    assert "note" not in preview["fields"]

    applied = await runtime.planner.apply(planned.id)
    assert fake.result_updates == [
        {"id": "result_1", "body": {"schemaFields": {"assayfield_ct": {"value": 15}}}}
    ]
    assert applied["journal"]["status"] == "succeeded"
    assert applied["journal"]["result"]["state"] == "updated"
    assert applied["journal"]["result"]["updated"] == [{"id": "result_1", "http_status": 200}]
    assert any(
        event["stage"] == "update_result" and event["details"]["http_status"] == 200
        for event in applied["journal"]["events"]
    )
    status = await runtime.planner.status(planned.id)
    assert status["journal"]["result"]["state"] == "updated"
    assert "remote_task" not in status
    with pytest.raises(ConflictError, match="already been applied"):
        await runtime.planner.apply(planned.id)


async def test_apply_uses_the_planned_values_when_the_csv_changes_later(
    runtime, fake, tmp_path
) -> None:
    fake.results = [make_result()]
    path = write_csv(tmp_path / "assay.csv", ["id", "Ct"], [["result_1", "15"]])
    planned = await plan_csv(runtime, path, {"Ct": "Ct"})
    path.write_text("id,Ct\nresult_1,99\n", encoding="utf-8")
    await runtime.planner.apply(planned.id)
    assert fake.result_updates[0]["body"]["schemaFields"]["assayfield_ct"]["value"] == 15


async def test_unmodified_export_round_trips_without_a_write(runtime, fake, tmp_path) -> None:
    fake.results = [make_result(note="")]
    path = tmp_path / "assay.csv"
    await export_dataset(
        runtime.service,
        kind="assay-results",
        path=str(path),
        file_format="csv",
        entry=ENTRY_ID,
    )
    with pytest.raises(ValidationError, match="did not change"):
        await plan_csv(runtime, path, {"Ct": "Ct", "Note": "Note"})
    assert fake.writes() == []


async def test_edited_export_updates_only_the_changed_display_name(runtime, fake, tmp_path) -> None:
    fake.results = [make_result()]
    path = tmp_path / "assay.csv"
    await export_dataset(
        runtime.service,
        kind="assay-results",
        path=str(path),
        file_format="csv",
        entry=ENTRY_ID,
    )
    text = path.read_text(encoding="utf-8")
    path.write_text(text.replace(",12,n", ",15,n"), encoding="utf-8")
    planned = await plan_csv(runtime, path, {"Ct": "Ct", "Note": "Note"})
    assert planned.after["results"][0]["schema_fields"] == {"assayfield_ct": {"value": 15}}
    await runtime.planner.apply(planned.id)
    assert fake.result_updates[0]["body"] == {"schemaFields": {"assayfield_ct": {"value": 15}}}


async def test_v2_shaped_result_uses_entry_schema_and_fields(runtime, fake, tmp_path) -> None:
    fake.results = [
        {
            "id": "result_1",
            "entryId": ENTRY_ID,
            "schemaId": "assaysch_abc123",
            "modifiedAt": MODIFIED,
            "fields": {"ct": {"value": 12}, "note": {"value": "n"}},
        }
    ]
    path = write_csv(tmp_path / "assay.csv", ["id", "Ct"], [["result_1", "9"]])
    planned = await plan_csv(runtime, path, {"Ct": "ct"})
    assert planned.before["results"][0]["fields"]["ct"]["value"] == 12
    await runtime.planner.apply(planned.id)
    assert fake.result_updates[0]["body"]["schemaFields"] == {"assayfield_ct": {"value": 9}}


async def test_blank_cell_clears_a_present_value_and_skips_an_empty_one(
    runtime, fake, tmp_path
) -> None:
    fake.results = [make_result(note="n"), make_result("result_2", note="")]
    path = write_csv(
        tmp_path / "assay.csv",
        ["id", "Note"],
        [["result_1", ""], ["result_2", ""]],
    )
    planned = await plan_csv(runtime, path, {"Note": "Note"})
    assert planned.after["results"] == [
        {
            "row": 2,
            "id": "result_1",
            "schema_id": "assaysch_abc123",
            "fields": {
                "note": {
                    "field_definition_id": "assayfield_note",
                    "system_name": "note",
                    "name": "Note",
                    "value": None,
                }
            },
            "schema_fields": {"assayfield_note": {"value": None}},
        }
    ]
    assert any("result_2" in warning for warning in planned.warnings)
    await runtime.planner.apply(planned.id)
    assert fake.result_updates[0]["body"] == {"schemaFields": {"assayfield_note": {"value": None}}}


async def test_float_boolean_date_and_multi_cells_keep_their_types(runtime, fake, tmp_path) -> None:
    fake.field_defs["assaysch_abc123"].extend(
        [
            {
                "id": "assayfield_od",
                "systemName": "od",
                "name": "OD",
                "__typename": "FloatFieldDefinition",
                "derivationType": "DIRECT",
            },
            {
                "id": "assayfield_ok",
                "systemName": "ok",
                "name": "OK",
                "__typename": "BooleanFieldDefinition",
                "derivationType": "DIRECT",
            },
            {
                "id": "assayfield_day",
                "systemName": "day",
                "name": "Day",
                "__typename": "DateFieldDefinition",
                "derivationType": "DIRECT",
            },
            {
                "id": "assayfield_tags",
                "systemName": "tags",
                "name": "Tags",
                "__typename": "TextFieldDefinition",
                "isMulti": True,
                "derivationType": "DIRECT",
            },
        ]
    )
    fake.results = [
        make_result(
            extra=[
                _field("assayfield_od", {"__typename": "DecimalValue", "value": "2.5"}),
                _field("assayfield_ok", {"__typename": "BooleanValue", "value": True}),
                _field("assayfield_day", {"__typename": "DateValue", "value": "2026-01-01"}),
                _field("assayfield_tags", {"__typename": "TextListValue", "value": ["a"]}),
            ]
        )
    ]
    unchanged = write_csv(tmp_path / "same.csv", ["id", "OD"], [["result_1", "2.5"]])
    with pytest.raises(ValidationError, match="did not change"):
        await plan_csv(runtime, unchanged, {"OD": "OD"})

    path = write_csv(
        tmp_path / "assay.csv",
        ["id", "OD", "OK", "Day", "Tags"],
        [["result_1", "3.5", "false", "2026-04-01", '["a","b"]']],
    )
    planned = await plan_csv(runtime, path, {"OD": "OD", "OK": "OK", "Day": "Day", "Tags": "Tags"})
    assert planned.after["results"][0]["schema_fields"] == {
        "assayfield_od": {"value": 3.5},
        "assayfield_ok": {"value": False},
        "assayfield_day": {"value": "2026-04-01"},
        "assayfield_tags": {"value": ["a", "b"]},
    }


@pytest.mark.parametrize(
    ("mutate", "match"),
    [
        ("version", "version"),
        ("entry", "entry"),
        ("value", "field values"),
        ("definition", "field definitions"),
    ],
)
async def test_apply_rechecks_the_version_before_any_patch(
    runtime, fake, tmp_path, mutate, match
) -> None:
    fake.results = [make_result()]
    path = write_csv(tmp_path / "assay.csv", ["id", "Ct"], [["result_1", "15"]])
    planned = await plan_csv(runtime, path, {"Ct": "Ct"})
    record = fake.results[0]
    if mutate == "version":
        record["modifiedAt"] = "2026-08-01T00:00:00Z"
    elif mutate == "entry":
        record["entry"] = {"id": "etr_other", "__typename": "Entry"}
    elif mutate == "value":
        record["schemaFields"][0]["value"] = _integer(13)
    else:
        fake.field_defs["assaysch_abc123"][0]["isRequired"] = False
    with pytest.raises(ConflictError, match=match):
        await runtime.planner.apply(planned.id)
    assert fake.writes() == []


async def test_apply_reports_a_result_that_disappeared(runtime, fake, tmp_path) -> None:
    fake.results = [make_result()]
    path = write_csv(tmp_path / "assay.csv", ["id", "Ct"], [["result_1", "15"]])
    planned = await plan_csv(runtime, path, {"Ct": "Ct"})
    fake.results.clear()
    with pytest.raises(ConflictError, match="not found"):
        await runtime.planner.apply(planned.id)
    assert fake.writes() == []


async def test_a_later_patch_failure_keeps_the_earlier_write(runtime, fake, tmp_path) -> None:
    fake.results = [make_result(), make_result("result_2", ct=3)]
    path = write_csv(
        tmp_path / "assay.csv",
        ["id", "Ct"],
        [["result_1", "15"], ["result_2", "4"]],
    )
    planned = await plan_csv(runtime, path, {"Ct": "Ct"})
    fake.fail("PATCH", "/result/", 400, skip=1)
    with pytest.raises(PartialCompletionError, match="later step failed"):
        await runtime.planner.apply(planned.id)
    assert [item["id"] for item in fake.result_updates] == ["result_1"]
    journal = runtime.repository.load_journal(planned.id)
    assert journal.status.value == "partial"


def _error_text(exc: ValidationError) -> str:
    nested = exc.details.get("errors") or []
    return exc.message + " " + " ".join(str(item.get("message", "")) for item in nested)


@pytest.mark.parametrize(
    ("setup", "rows", "column_map", "id_column", "match", "result_reads"),
    [
        ("ready", [["result_missing", "1"]], {"Ct": "Ct"}, "id", "Unknown result", True),
        ("other-entry", [["result_1", "1"]], {"Ct": "Ct"}, "id", "not on the selected entry", True),
        ("no-version", [["result_1", "1"]], {"Ct": "Ct"}, "id", "no modifiedAt", True),
        ("no-schema", [["result_1", "1"]], {"Ct": "Ct"}, "id", "no schema", True),
        ("ready", [["result_1", "1"]], {"Nope": "Nope"}, "id", "not in the header", False),
        ("unknown-field", [["result_1", "1"]], {"Nope": "Nope"}, "id", "Unknown column", True),
        ("blank-id", [["", "1"]], {"Ct": "Ct"}, "id", "Result id is blank", False),
        ("ready", [["result_1", "1"], ["result_1", "2"]], {"Ct": "Ct"}, "id", "is repeated", False),
        ("ready", [["result_1", "high"]], {"Ct": "Ct"}, "id", "must be an integer", True),
        ("ready", [["result_1", ""]], {"Ct": "Ct"}, "id", "cannot be cleared", True),
        ("ready", [["result_1", "1"]], {"Ct": "Ct"}, "identifier", "not in the header", False),
    ],
)
async def test_bad_rows_and_mappings_write_nothing(
    runtime, fake, tmp_path, setup, rows, column_map, id_column, match, result_reads
) -> None:
    record = make_result()
    if setup == "other-entry":
        record = make_result(entry="etr_other")
    elif setup == "no-version":
        record = make_result(modified=None)
    elif setup == "no-schema":
        record = make_result(schema=None)
    fake.results = [record]
    headers = ["id", "Nope"] if setup == "unknown-field" else ["id", "Ct"]
    path = write_csv(tmp_path / "assay.csv", headers, rows)
    with pytest.raises(ValidationError) as caught:
        await plan_csv(runtime, path, column_map, id_column=id_column)
    assert match in _error_text(caught.value)
    assert fake.writes() == []
    assert _no_result_reads(fake) is not result_reads


async def test_ambiguous_archived_computed_and_unsupported_fields_are_row_errors(
    runtime, fake, tmp_path
) -> None:
    fake.field_defs["assaysch_abc123"].extend(
        [
            {
                "id": "assayfield_note2",
                "systemName": "note_2",
                "name": "Note",
                "__typename": "TextFieldDefinition",
                "derivationType": "DIRECT",
            },
            {
                "id": "assayfield_blob",
                "systemName": "blob",
                "name": "Blob",
                "__typename": "BlobFieldDefinition",
                "derivationType": "DIRECT",
            },
        ]
    )
    fake.results = [make_result()]
    path = write_csv(tmp_path / "assay.csv", ["id", "Note", "Blob"], [["result_1", "x", "y"]])
    with pytest.raises(ValidationError) as ambiguous:
        await plan_csv(runtime, path, {"Note": "Note", "Blob": "Blob"})
    assert "more than one field" in _error_text(ambiguous.value)
    assert fake.writes() == []

    fake.field_defs["assaysch_abc123"] = [
        item for item in fake.field_defs["assaysch_abc123"] if item["id"] != "assayfield_note2"
    ]
    note = next(
        item for item in fake.field_defs["assaysch_abc123"] if item["id"] == "assayfield_note"
    )
    note["archived"] = True
    with pytest.raises(ValidationError) as archived:
        await plan_csv(runtime, path, {"Note": "Note"})
    assert "Archived" in _error_text(archived.value)

    note["archived"] = False
    note["derivationType"] = "COMPUTED"
    with pytest.raises(ValidationError) as computed:
        await plan_csv(runtime, path, {"Note": "Note"})
    assert "Computed or snapshot" in _error_text(computed.value)

    note["derivationType"] = "DIRECT"
    blob_only = write_csv(tmp_path / "blob.csv", ["id", "Blob"], [["result_1", "y"]])
    with pytest.raises(ValidationError) as unsupported:
        await plan_csv(runtime, blob_only, {"Blob": "Blob"})
    assert "cannot be updated" in _error_text(unsupported.value)
    assert fake.writes() == []


async def test_two_columns_cannot_map_to_the_same_field(runtime, fake, tmp_path) -> None:
    fake.results = [make_result()]
    path = write_csv(tmp_path / "assay.csv", ["id", "Ct", "Cq"], [["result_1", "15", "16"]])
    with pytest.raises(ValidationError) as caught:
        await plan_csv(runtime, path, {"Ct": "ct", "Cq": "assayfield_ct"})
    assert "same field" in _error_text(caught.value)
    assert fake.writes() == []


async def test_bad_date_multi_and_boolean_cells_are_reported(runtime, fake, tmp_path) -> None:
    fake.field_defs["assaysch_abc123"].extend(
        [
            {
                "id": "assayfield_day",
                "systemName": "day",
                "name": "Day",
                "__typename": "DateFieldDefinition",
                "derivationType": "DIRECT",
            },
            {
                "id": "assayfield_tags",
                "systemName": "tags",
                "name": "Tags",
                "__typename": "TextFieldDefinition",
                "isMulti": True,
                "derivationType": "DIRECT",
            },
            {
                "id": "assayfield_ok",
                "systemName": "ok",
                "name": "OK",
                "__typename": "BooleanFieldDefinition",
                "derivationType": "DIRECT",
            },
        ]
    )
    fake.results = [make_result()]
    path = write_csv(
        tmp_path / "assay.csv",
        ["id", "Day", "Tags", "OK"],
        [["result_1", "April 1", "a,b", "yes"]],
    )
    with pytest.raises(ValidationError, match="rejected before anything was written") as caught:
        await plan_csv(runtime, path, {"Day": "Day", "Tags": "Tags", "OK": "OK"})
    messages = [item["message"] for item in caught.value.details["errors"]]
    assert any("wrong type" in message for message in messages)
    assert any("must be JSON" in message for message in messages)
    assert any("true or false" in message for message in messages)
    assert fake.writes() == []


async def test_structural_csv_errors_write_nothing(runtime, fake, tmp_path) -> None:
    fake.results = [make_result()]
    missing = tmp_path / "missing.csv"
    with pytest.raises(ValidationError, match="was not found"):
        await plan_csv(runtime, missing, {"Ct": "Ct"})

    binary = tmp_path / "binary.csv"
    binary.write_bytes(b"\xff\xfe")
    with pytest.raises(ValidationError, match="UTF-8"):
        await plan_csv(runtime, binary, {"Ct": "Ct"})

    empty = tmp_path / "empty.csv"
    empty.write_text("", encoding="utf-8")
    with pytest.raises(ValidationError, match="no header"):
        await plan_csv(runtime, empty, {"Ct": "Ct"})

    header_only = write_csv(tmp_path / "header.csv", ["id", "Ct"], [])
    with pytest.raises(ValidationError, match="no result rows"):
        await plan_csv(runtime, header_only, {"Ct": "Ct"})

    repeated = tmp_path / "repeated.csv"
    repeated.write_text("id,Ct,Ct\nresult_1,1,2\n", encoding="utf-8")
    with pytest.raises(ValidationError, match="repeats a column"):
        await plan_csv(runtime, repeated, {"Ct": "Ct"})

    extra = write_csv(tmp_path / "extra.csv", ["id", "Ct"], [["result_1", "15", "nope"]])
    with pytest.raises(ValidationError) as extra_row:
        await plan_csv(runtime, extra, {"Ct": "Ct"})
    assert "more cells" in _error_text(extra_row.value)

    blank_lines = tmp_path / "blanks.csv"
    blank_lines.write_text("id,Ct\n\nresult_1,15\n\n", encoding="utf-8")
    planned = await plan_csv(runtime, blank_lines, {"Ct": "Ct"})
    assert planned.after["results"][0]["row"] == 3
    assert fake.writes() == []


async def test_too_many_rows_and_truncated_row_errors(runtime, fake, tmp_path) -> None:
    fake.results = [make_result()]
    wide = write_csv(
        tmp_path / "wide.csv",
        ["id", "Ct"],
        [[f"result_{index}", "1"] for index in range(501)],
    )
    with pytest.raises(ValidationError, match="at most 500") as limited:
        await plan_csv(runtime, wide, {"Ct": "Ct"})
    assert limited.value.details["rows"] == 501
    assert _no_result_reads(fake)

    fake.calls.clear()
    blanks = write_csv(tmp_path / "blanks.csv", ["id", "Ct"], [["", "1"]] * 51)
    with pytest.raises(ValidationError, match="rejected before anything was written") as caught:
        await plan_csv(runtime, blanks, {"Ct": "Ct"})
    assert caught.value.details["error_count"] == 51
    assert caught.value.details["truncated"] is True
    assert len(caught.value.details["errors"]) == 50
    assert _no_result_reads(fake)
    assert fake.writes() == []


async def test_request_shape_is_validated_before_a_write(runtime, fake, tmp_path) -> None:
    present = write_csv(tmp_path / "assay.csv", ["id", "Ct"], [["result_1", "15"]])
    with pytest.raises(ValidationError, match="results family"):
        await runtime.planner.plan(ENTRY_ID, "document", _request(present, {"Ct": "Ct"}))
    assert fake.calls == []
    with pytest.raises(ValidationError, match="does not use table_id"):
        await runtime.planner.plan(
            ENTRY_ID,
            "results",
            {**_request(present, {"Ct": "Ct"}), "table_id": "strtbl_example"},
        )

    fake.results = [make_result()]
    with pytest.raises(ValidationError, match="csv_path must be"):
        await runtime.planner.plan(
            ENTRY_ID, "results", {"operation": "update", "csv_path": "", "id_column": "id"}
        )
    with pytest.raises(ValidationError, match="id_column must"):
        await runtime.planner.plan(
            ENTRY_ID,
            "results",
            {"operation": "update", "csv_path": str(present), "column_map": {"Ct": "Ct"}},
        )
    with pytest.raises(ValidationError, match="column_map must"):
        await runtime.planner.plan(
            ENTRY_ID,
            "results",
            {"operation": "update", "csv_path": str(present), "id_column": "id", "column_map": {}},
        )
    with pytest.raises(ValidationError, match="cannot also be mapped"):
        await plan_csv(runtime, present, {"id": "id", "Ct": "Ct"}, id_column="id")
    with pytest.raises(ValidationError, match="more than once"):
        await runtime.planner.plan(
            ENTRY_ID,
            "results",
            {
                "operation": "update",
                "csv_path": str(present),
                "id_column": "id",
                "column_map": {"Ct": "Ct", " Ct": "Note"},
            },
        )
    with pytest.raises(ValidationError, match="field names or field-definition ids"):
        await runtime.planner.plan(
            ENTRY_ID,
            "results",
            {
                "operation": "update",
                "csv_path": str(present),
                "id_column": "id",
                "column_map": {"Ct": ""},
            },
        )
    assert fake.writes() == []


async def test_missing_result_schema_is_reported(runtime, fake, tmp_path) -> None:
    fake.results = [make_result(schema="assaysch_missing")]
    path = write_csv(tmp_path / "assay.csv", ["id", "Ct"], [["result_1", "15"]])
    with pytest.raises(ValidationError) as caught:
        await plan_csv(runtime, path, {"Ct": "Ct"})
    assert "schema was not found" in _error_text(caught.value)
    assert fake.writes() == []


async def test_result_not_on_the_entry_says_pending_results_must_be_accepted(
    runtime, fake, tmp_path
) -> None:
    fake.results = [make_result(entry=None)]
    path = write_csv(tmp_path / "assay.csv", ["id", "Ct"], [["result_1", "1"]])
    with pytest.raises(ValidationError) as caught:
        await plan_csv(runtime, path, {"Ct": "Ct"})
    text = _error_text(caught.value)
    assert "not on the selected entry" in text
    assert "pending" in text
    assert "plug icon" in text
    assert fake.writes() == []
