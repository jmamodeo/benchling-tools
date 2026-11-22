from __future__ import annotations

import pytest
from fake_benchling import ENTRY_ID, TABLE_PART, TEXT_PART

from benchling_tools.errors import (
    ConflictError,
    PartialCompletionError,
    UnsupportedError,
    UpstreamError,
    ValidationError,
)


async def plan(runtime, request):
    return await runtime.planner.plan(ENTRY_ID, "document", request)


def mutations(fake):
    return [patch["partsMutation"] for patch in fake.entry_patches]


@pytest.mark.parametrize(
    "request_body",
    [
        {"operation": "insert", "parts": []},
        {"operation": "insert"},
        {"operation": "insert", "parts": ["text"]},
        {"operation": "insert", "parts": [{"text": "no content type"}]},
        {"operation": "insert", "parts": [{"contentType": "text"}], "position": "docpart_nope"},
        {"operation": "delete", "from_part_id": "docpart_nope"},
        {"operation": "delete", "from_part_id": TEXT_PART, "to_part_id": "docpart_nope"},
        {"operation": "delete", "from_part_id": "docpart_text2", "to_part_id": TEXT_PART},
        {"operation": "move", "from_part_id": TEXT_PART, "position": "docpart_nope"},
        {
            "operation": "move",
            "from_part_id": "docpart_sec1",
            "to_part_id": "docpart_sec2",
            "position": TEXT_PART,
        },
        {"operation": "replace_text", "part_id": "docpart_nope", "text": "x"},
        {"operation": "replace_text", "part_id": TEXT_PART},
        {"operation": "replace_text", "part_id": TEXT_PART, "text": 7},
        {"operation": "reorder"},
        {},
    ],
)
async def test_invalid_document_specs_are_rejected_before_any_write(
    runtime, fake, request_body
) -> None:
    with pytest.raises(ValidationError):
        await plan(runtime, request_body)
    assert fake.writes() == []
    assert not list((runtime.repository.config.plans_dir).glob("*.json"))


async def test_replace_text_requires_a_text_part(runtime, fake) -> None:
    with pytest.raises(UnsupportedError):
        await plan(runtime, {"operation": "replace_text", "part_id": "docpart_sec1", "text": "x"})


async def test_insert_preview_describes_change_without_writing(runtime, fake) -> None:
    new_part = {"contentType": "text", "text": "inserted"}
    result = await plan(
        runtime, {"operation": "insert", "position": TEXT_PART, "parts": [new_part]}
    )
    assert result.before["parts"] == []
    assert result.after["parts"] == [new_part]
    assert result.preconditions["entry_version"] == "ver_1"
    assert result.preconditions["part_ids"] == fake.part_ids()
    assert result.warnings == []
    assert fake.writes() == []


async def test_insert_applies_one_versioned_mutation_and_discovers_new_part_id(
    runtime, fake
) -> None:
    planned = await plan(
        runtime,
        {
            "operation": "insert",
            "position": "beginning_of_document",
            "parts": [{"contentType": "text", "text": "intro"}],
        },
    )
    applied = await runtime.planner.apply(planned.id)
    assert mutations(fake) == [
        {
            "operationType": "insert",
            "version": "ver_1",
            "position": "beginning_of_document",
            "parts": [{"contentType": "text", "text": "intro"}],
        }
    ]
    assert fake.part_ids()[0] == "docpart_new1"
    result = applied["journal"]["result"]
    assert result["changed_part_ids"] == ["docpart_new1"]
    assert result["part_id_discovery"] == {"complete": True, "ids": ["docpart_new1"]}
    assert applied["journal"]["status"] == "succeeded"


async def test_insert_defaults_to_end_of_document(runtime, fake) -> None:
    planned = await plan(
        runtime, {"operation": "insert", "parts": [{"contentType": "text", "text": "tail"}]}
    )
    assert planned.after["position"] == "end_of_document"
    await runtime.planner.apply(planned.id)
    assert fake.part_ids()[-1] == "docpart_new1"


async def test_discovery_failure_is_reported_without_failing_the_applied_write(
    runtime, fake
) -> None:
    planned = await plan(
        runtime, {"operation": "insert", "parts": [{"contentType": "text", "text": "tail"}]}
    )
    fake.fail("GET", "/parts/items", 403)
    applied = await runtime.planner.apply(planned.id)
    discovery = applied["journal"]["result"]["part_id_discovery"]
    assert discovery["complete"] is False
    assert discovery["error"]["code"] == "access_denied"
    assert discovery["ids"] == []
    assert applied["journal"]["status"] == "succeeded"


async def test_discovery_survives_unexpected_local_errors(runtime, fake) -> None:
    planned = await plan(
        runtime, {"operation": "insert", "parts": [{"contentType": "text", "text": "tail"}]}
    )
    fake.fail("GET", "/parts/items", 0)
    applied = await runtime.planner.apply(planned.id)
    discovery = applied["journal"]["result"]["part_id_discovery"]
    assert discovery["error"] == {"code": "local_discovery_error", "type": "RuntimeError"}


async def test_delete_range_removes_inclusive_parts_and_reports_their_ids(runtime, fake) -> None:
    planned = await plan(
        runtime, {"operation": "delete", "from_part_id": "docpart_sec2", "to_part_id": TABLE_PART}
    )
    assert [part["id"] for part in planned.before["parts"]] == ["docpart_sec2", TABLE_PART]
    assert planned.after["parts"] == []
    applied = await runtime.planner.apply(planned.id)
    assert fake.part_ids() == ["docpart_sec1", TEXT_PART, "docpart_struct1", "docpart_text2"]
    assert mutations(fake)[0]["sourceRange"] == {
        "start": "docpart_sec2",
        "end": TABLE_PART,
    }
    assert applied["journal"]["result"]["changed_part_ids"] == ["docpart_sec2", TABLE_PART]


async def test_delete_single_part_defaults_end_of_range_to_start(runtime, fake) -> None:
    planned = await plan(runtime, {"operation": "delete", "from_part_id": "docpart_text2"})
    await runtime.planner.apply(planned.id)
    assert mutations(fake)[0]["sourceRange"] == {
        "start": "docpart_text2",
        "end": "docpart_text2",
    }
    assert "docpart_text2" not in fake.part_ids()


async def test_move_that_would_not_start_with_a_section_is_rejected_before_apply(
    runtime, fake
) -> None:
    with pytest.raises(ValidationError, match="Entry must start with a section"):
        await plan(
            runtime,
            {
                "operation": "move",
                "from_part_id": TEXT_PART,
                "position": "beginning_of_document",
            },
        )
    with pytest.raises(ValidationError, match="Entry must start with a section"):
        await plan(
            runtime,
            {
                "operation": "move",
                "from_part_id": "docpart_sec1",
                "position": "end_of_document",
            },
        )
    with pytest.raises(ValidationError, match="Entry must start with a section"):
        await plan(
            runtime,
            {
                "operation": "move",
                "from_part_id": "docpart_text2",
                "position": "docpart_sec1",
            },
        )
    assert fake.writes() == []
    assert not list(runtime.repository.config.plans_dir.glob("*.json"))
    parked = await plan(
        runtime,
        {"operation": "move", "from_part_id": "docpart_sec1", "position": TEXT_PART},
    )
    assert parked.after["position"] == TEXT_PART
    assert fake.writes() == []


async def test_move_range_to_destination(runtime, fake) -> None:
    planned = await plan(
        runtime,
        {
            "operation": "move",
            "from_part_id": "docpart_sec2",
            "to_part_id": "docpart_struct1",
            "position": "beginning_of_document",
        },
    )
    assert planned.after["position"] == "beginning_of_document"
    await runtime.planner.apply(planned.id)
    assert fake.part_ids() == [
        "docpart_sec2",
        TABLE_PART,
        "docpart_struct1",
        "docpart_sec1",
        TEXT_PART,
        "docpart_text2",
    ]
    mutation = mutations(fake)[0]
    assert mutation["operationType"] == "move"
    assert mutation["sourceRange"] == {"start": "docpart_sec2", "end": "docpart_struct1"}
    assert mutation["destinationPosition"] == "beginning_of_document"


async def test_replace_text_preserves_existing_formatting_unless_overridden(runtime, fake) -> None:
    kept = await plan(runtime, {"operation": "replace_text", "part_id": TEXT_PART, "text": "new"})
    assert kept.after["part"]["formats"] == [{"start": 0, "end": 3, "properties": ["BOLD"]}]
    assert kept.after["part"]["contentType"] == "text"
    assert kept.warnings and "partially complete" in kept.warnings[0]
    overridden = await plan(
        runtime,
        {
            "operation": "replace_text",
            "part_id": TEXT_PART,
            "text": "new",
            "formats": [],
            "indentation": 2,
        },
    )
    assert overridden.after["part"]["formats"] == []
    assert overridden.after["part"]["indentation"] == 2


async def test_replace_text_accepts_flattened_v3_text_parts(runtime, fake) -> None:
    """Current V3 responses flatten the part: type and formatting sit beside the id."""
    flattened = {
        "id": "docpart_flat1",
        "__typename": "DocumentPartText",
        "text": "flat",
        "formats": [{"type": "italic", "start": 0, "end": 2}],
        "links": [],
        "mentions": [],
        "indentation": 1,
    }
    fake.parts.append(flattened)
    planned = await plan(
        runtime, {"operation": "replace_text", "part_id": "docpart_flat1", "text": "edited"}
    )
    assert planned.after["part"] == {
        "contentType": "text",
        "text": "edited",
        "formats": [{"type": "italic", "start": 0, "end": 2}],
        "links": [],
        "mentions": [],
        "indentation": 1,
    }
    assert fake.writes() == []


async def test_replace_text_inserts_replacement_then_deletes_original(runtime, fake) -> None:
    planned = await plan(
        runtime, {"operation": "replace_text", "part_id": TEXT_PART, "text": "after edit"}
    )
    applied = await runtime.planner.apply(planned.id)
    insert, delete = mutations(fake)
    assert insert["operationType"] == "insert" and insert["version"] == "ver_1"
    assert insert["position"] == TEXT_PART
    assert insert["parts"][0]["text"] == "after edit"
    assert delete["operationType"] == "delete" and delete["version"] == "ver_2"
    assert delete["sourceRange"] == {"start": TEXT_PART, "end": TEXT_PART}
    assert TEXT_PART not in fake.part_ids()
    assert fake.part_text("docpart_new1") == "after edit"
    assert applied["journal"]["result"]["changed_part_ids"] == [TEXT_PART, "docpart_new1"]
    stages = [event["stage"] for event in applied["journal"]["events"]]
    assert stages == ["apply", "insert_replacement", "delete_original", "apply"]
    assert applied["journal"]["events"][1]["details"]["http_status"] == 200
    assert applied["journal"]["events"][2]["details"]["http_status"] == 200


async def test_replace_text_partial_failure_keeps_inserted_part_and_journals_it(
    runtime, fake
) -> None:
    planned = await plan(
        runtime, {"operation": "replace_text", "part_id": TEXT_PART, "text": "after edit"}
    )
    fake.fail("PATCH", f"/entry/{ENTRY_ID}", 409, skip=1)
    with pytest.raises(PartialCompletionError) as caught:
        await runtime.planner.apply(planned.id)
    assert caught.value.details["cause"] == "The Benchling resource changed since it was read"
    assert {TEXT_PART, "docpart_new1"} <= set(fake.part_ids())
    status = await runtime.planner.status(planned.id)
    assert status["journal"]["status"] == "partial"
    assert [event["stage"] for event in status["journal"]["events"]] == [
        "apply",
        "insert_replacement",
        "apply",
    ]
    assert status["journal"]["events"][1]["details"]["http_status"] == 200
    assert status["journal"]["events"][2]["details"]["http_status"] == 409
    with pytest.raises(ConflictError):
        await runtime.planner.apply(planned.id)


async def test_replace_text_local_failure_after_first_write_is_partial(runtime, fake) -> None:
    planned = await plan(
        runtime, {"operation": "replace_text", "part_id": TEXT_PART, "text": "after edit"}
    )
    fake.fail("PATCH", f"/entry/{ENTRY_ID}", 0, skip=1)
    with pytest.raises(PartialCompletionError) as caught:
        await runtime.planner.apply(planned.id)
    assert caught.value.details["type"] == "RuntimeError"
    assert (await runtime.planner.status(planned.id))["journal"]["status"] == "partial"


async def test_local_failure_before_any_write_is_a_failed_plan(runtime, fake) -> None:
    planned = await plan(runtime, {"operation": "delete", "from_part_id": TEXT_PART})
    fake.fail("PATCH", f"/entry/{ENTRY_ID}", 0)
    with pytest.raises(UpstreamError) as caught:
        await runtime.planner.apply(planned.id)
    assert caught.value.details["type"] == "RuntimeError"
    journal = (await runtime.planner.status(planned.id))["journal"]
    assert journal["status"] == "failed"
    assert "http_status" not in journal["events"][-1]["details"]


async def test_server_error_on_write_leaves_plan_in_unknown_state(runtime, fake) -> None:
    planned = await plan(runtime, {"operation": "delete", "from_part_id": TEXT_PART})
    fake.fail("PATCH", f"/entry/{ENTRY_ID}", 503)
    with pytest.raises(PartialCompletionError) as caught:
        await runtime.planner.apply(planned.id)
    assert caught.value.details["journal"]["status"] == "unknown"
    unknown = caught.value.details["journal"]["events"][-1]
    assert unknown["details"]["http_status"] == 503
    # A 5xx on a write is never retried.
    assert len(fake.writes()) == 1
    with pytest.raises(ConflictError):
        await runtime.planner.apply(planned.id)


async def test_apply_refuses_when_entry_changed_since_preview(runtime, fake) -> None:
    planned = await plan(runtime, {"operation": "delete", "from_part_id": TEXT_PART})
    fake.entry_version += 1
    with pytest.raises(ConflictError) as caught:
        await runtime.planner.apply(planned.id)
    assert caught.value.details == {"expected": "ver_1", "actual": "ver_2"}
    assert fake.writes() == []
    assert (await runtime.planner.status(planned.id))["journal"]["status"] == "failed"


async def test_server_rejects_stale_version_mid_apply(runtime, fake) -> None:
    planned = await plan(runtime, {"operation": "delete", "from_part_id": TEXT_PART})
    fake.fail("PATCH", f"/entry/{ENTRY_ID}", 409)
    with pytest.raises(ConflictError):
        await runtime.planner.apply(planned.id)
    assert TEXT_PART in fake.part_ids()
    status = await runtime.planner.status(planned.id)
    assert status["journal"]["status"] == "failed"
    assert status["journal"]["events"][-1]["details"]["http_status"] == 409
