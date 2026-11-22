from __future__ import annotations

import pytest
from fake_benchling import ENTRY_ID, TABLE_PART

from benchling_tools.errors import (
    ConflictError,
    NotFoundError,
    PartialCompletionError,
    UnsupportedError,
    ValidationError,
)


async def plan(runtime, request, part_id=TABLE_PART):
    return await runtime.planner.plan(ENTRY_ID, "table", {"table_part_id": part_id, **request})


def cell(row, column, value):
    return {"row": row, "column": column, "value": value}


@pytest.mark.parametrize(
    ("request_body", "expected"),
    [
        ({"operation": "set_cells"}, "non-empty updates"),
        ({"operation": "set_cells", "updates": []}, "non-empty updates"),
        ({"operation": "set_cells", "updates": [{"row": 0, "column": 0}]}, "coordinates"),
        ({"operation": "set_cells", "updates": [cell(-1, 0, "x")]}, "coordinates"),
        ({"operation": "set_cells", "updates": [cell(0, -1, "x")]}, "coordinates"),
        ({"operation": "set_cells", "updates": [cell(True, 0, "x")]}, "coordinates"),
        ({"operation": "set_cells", "updates": [cell(0, 1.5, "x")]}, "coordinates"),
        ({"operation": "set_cells", "updates": ["A1"]}, "coordinates"),
        ({"operation": "set_cells", "updates": [cell(3, 0, "x")]}, "outside the table"),
        ({"operation": "set_cells", "updates": [cell(0, 2, "x")]}, "outside the table"),
        ({"operation": "set_cells", "updates": [cell(0, 0, "a"), cell(0, 0, "b")]}, "same cell"),
        ({"operation": "append_rows"}, "non-empty rows"),
        ({"operation": "append_rows", "rows": []}, "non-empty rows"),
        ({"operation": "append_rows", "rows": [["only one"]]}, "wrong number of cells"),
        ({"operation": "append_rows", "rows": [["a", "b", "c"]]}, "wrong number of cells"),
        ({"operation": "append_rows", "rows": ["flat"]}, "must contain cells"),
        ({"operation": "append_rows", "rows": [{"cells": "ab"}]}, "must contain cells"),
        ({"operation": "replace"}, "Unknown table operation"),
        ({"operation": "sort"}, "Unknown table operation"),
        ({}, "Unknown table operation"),
    ],
)
async def test_invalid_table_specs_are_rejected_without_writing(
    runtime, fake, request_body, expected
) -> None:
    with pytest.raises(ValidationError) as caught:
        await plan(runtime, request_body)
    assert expected in caught.value.message
    assert fake.writes() == []


async def test_table_part_id_is_required(runtime) -> None:
    with pytest.raises(ValidationError, match="table_part_id"):
        await runtime.planner.plan(ENTRY_ID, "table", {"operation": "append_rows"})


async def test_structured_tables_cannot_be_edited(runtime) -> None:
    with pytest.raises(UnsupportedError, match="unstructured"):
        await plan(
            runtime, {"operation": "append_rows", "rows": [["a", "b"]]}, part_id="docpart_struct1"
        )


async def test_table_part_of_another_entry_is_not_found(runtime) -> None:
    with pytest.raises(NotFoundError):
        await plan(
            runtime, {"operation": "append_rows", "rows": [["a", "b"]]}, part_id="docpart_elsewhere"
        )


async def test_text_part_is_not_an_editable_table(runtime) -> None:
    with pytest.raises(UnsupportedError):
        await plan(
            runtime, {"operation": "append_rows", "rows": [["a", "b"]]}, part_id="docpart_text1"
        )


async def test_writing_into_a_merged_child_cell_is_unsupported(runtime) -> None:
    with pytest.raises(UnsupportedError, match="merged child"):
        await plan(runtime, {"operation": "set_cells", "updates": [cell(1, 1, "x")]})


async def test_set_cells_preview_coerces_values_and_keeps_cell_state(runtime, fake) -> None:
    planned = await plan(
        runtime, {"operation": "set_cells", "updates": [cell(0, 1, 42), cell(2, 0, "D")]}
    )
    rows = planned.after["content"]["rows"]
    assert rows[0]["cells"][1]["inputValue"] == {"type": "text_value", "value": "42"}
    assert "evaluationValue" not in rows[0]["cells"][1]
    assert rows[0]["cells"][1]["format"] == {"backgroundColor": "#fff000"}
    assert rows[0]["cells"][1]["selectorId"] == "selector_1"
    assert rows[1]["cells"][0]["mergeRange"] == {"mergeRowSpan": 1, "mergeColSpan": 2}
    assert rows[2]["cells"][0]["inputValue"]["value"] == "D"
    assert planned.after["content"]["isEvaluated"] is False
    assert planned.before["content"]["isEvaluated"] is True
    assert planned.before["content"]["rows"][0]["cells"][1]["evaluationValue"]["value"] == "1"
    assert planned.preconditions == {"entry_version": "ver_1", "entry_id": ENTRY_ID}
    assert planned.guarantees == ["server-enforced document version check"]
    assert planned.warnings and "evaluat" in planned.warnings[0]
    assert fake.writes() == []


async def test_append_rows_accepts_lists_and_cell_objects(runtime) -> None:
    planned = await plan(
        runtime,
        {
            "operation": "append_rows",
            "rows": [
                ["D", 4],
                {
                    "cells": [
                        {"inputValue": {"type": "text_value", "value": "E"}},
                        {},
                    ]
                },
            ],
        },
    )
    rows = planned.after["content"]["rows"]
    assert len(rows) == 5
    assert rows[3]["cells"][1]["inputValue"]["value"] == "4"
    assert rows[4]["cells"][1] == {"inputValue": {"type": "text_value", "value": ""}}
    assert len(planned.before["content"]["rows"]) == 3


async def test_append_row_with_non_dict_cell_is_stringified(runtime) -> None:
    planned = await plan(runtime, {"operation": "append_rows", "rows": [{"cells": ["F", 6]}]})
    assert planned.after["content"]["rows"][3]["cells"][1]["inputValue"]["value"] == "6"


async def test_apply_sends_only_the_edited_cells(runtime, fake) -> None:
    planned = await plan(runtime, {"operation": "set_cells", "updates": [cell(0, 1, "3")]})
    applied = await runtime.planner.apply(planned.id)
    assert applied["journal"]["result"]["evaluated_values_cleared"] is True
    (patch,) = fake.entry_patches
    mutation = patch["partsMutation"]
    assert mutation == {
        "operationType": "update_table_cells",
        "version": "ver_1",
        "documentPartId": TABLE_PART,
        "cellUpdates": [
            {
                "rowIndex": 0,
                "columnIndex": 1,
                "inputValue": {"type": "text_value", "value": "3"},
            }
        ],
    }
    updated = next(
        event for event in applied["journal"]["events"] if event["stage"] == "update_table_cells"
    )
    assert updated["details"]["http_status"] == 200
    assert fake.table_content["rows"][0]["cells"][1]["inputValue"]["value"] == "3"
    assert fake.entry_version == 2
    assert [event["stage"] for event in applied["journal"]["events"]] == [
        "apply",
        "update_table_cells",
        "apply",
    ]


async def test_append_rows_apply_inserts_rows_on_the_entry(runtime, fake) -> None:
    planned = await plan(runtime, {"operation": "append_rows", "rows": [["D", "4"]]})
    await runtime.planner.apply(planned.id)
    (patch,) = fake.entry_patches
    mutation = patch["partsMutation"]
    assert mutation["operationType"] == "insert_table_rows"
    assert mutation["position"] == "end_of_table"
    assert mutation["version"] == "ver_1"
    assert mutation["rows"] == [
        {
            "cells": [
                {"columnIndex": 0, "inputValue": {"type": "text_value", "value": "D"}},
                {"columnIndex": 1, "inputValue": {"type": "text_value", "value": "4"}},
            ]
        }
    ]
    assert len(fake.table_content["rows"]) == 4
    assert fake.table_content["rows"][3]["cells"][0]["inputValue"]["value"] == "D"


async def test_apply_refuses_stale_entry_version_without_writing(runtime, fake) -> None:
    planned = await plan(runtime, {"operation": "append_rows", "rows": [["D", "4"]]})
    fake.entry_version += 1
    with pytest.raises(ConflictError) as caught:
        await runtime.planner.apply(planned.id)
    assert caught.value.message == "Entry version changed after the plan was created"
    assert caught.value.details == {"expected": "ver_1", "actual": "ver_2"}
    assert fake.writes() == []
    assert (await runtime.planner.status(planned.id))["journal"]["status"] == "failed"


async def test_server_side_version_mismatch_is_the_stale_plan_refusal(runtime, fake) -> None:
    planned = await plan(runtime, {"operation": "append_rows", "rows": [["D", "4"]]})
    fake.fail("PATCH", f"/entry/{ENTRY_ID}", 409)
    with pytest.raises(ConflictError) as caught:
        await runtime.planner.apply(planned.id)
    assert caught.value.message == "Entry version changed after the plan was created"
    assert caught.value.details["expected"] == "ver_1"
    assert caught.value.details["status"] == 409
    status = await runtime.planner.status(planned.id)
    assert status["journal"]["status"] == "failed"
    assert [event["status"] for event in status["journal"]["events"]] == ["started", "failed"]


async def test_unconfirmed_table_write_is_unknown(runtime, fake) -> None:
    planned = await plan(runtime, {"operation": "append_rows", "rows": [["D", "4"]]})
    fake.fail("PATCH", f"/entry/{ENTRY_ID}", 502)
    with pytest.raises(PartialCompletionError):
        await runtime.planner.apply(planned.id)
    assert (await runtime.planner.status(planned.id))["journal"]["status"] == "unknown"


async def test_table_part_version_mismatch_during_planning_is_a_conflict(runtime, fake) -> None:
    fake.parts[3]["versionId"] = "tver_other"
    with pytest.raises(ConflictError, match="while its target entry"):
        await plan(runtime, {"operation": "append_rows", "rows": [["D", "4"]]})
