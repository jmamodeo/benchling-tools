from __future__ import annotations

import asyncio

import pytest
from fake_benchling import ENTRY_ID, entry_web_url

from benchlingtk.errors import (
    ConflictError,
    NotFoundError,
    PartialCompletionError,
    UnsupportedError,
    ValidationError,
)

TABLE = "strtbl_example"


def record(**overrides):
    base = {"schemaId": "assaysch_abc123", "fields": {"ct": {"value": 12}}}
    return {**base, **overrides}


async def plan(runtime, records, table_id=TABLE):
    return await runtime.planner.plan(
        ENTRY_ID, "results", {"table_id": table_id, "assay_results": records}
    )


async def multi_table_plan(runtime, fake):
    fake.field_defs["assaysch_other"] = fake.field_defs["assaysch_abc123"]
    fake.extra_result_tables.append(
        {
            "type": "results_table",
            "apiId": "strtbl_other",
            "name": "Other results",
            "assayResultSchemaId": "assaysch_other",
            "columns": ["ct"],
        }
    )
    return await runtime.planner.plan(
        ENTRY_ID,
        "results",
        {
            "batches": [
                {"table_id": TABLE, "assay_results": [record()]},
                {
                    "table_id": "strtbl_other",
                    "assay_results": [
                        record(schemaId="assaysch_other", fields={"ct": {"value": 8}})
                    ],
                },
            ]
        },
    )


@pytest.mark.parametrize(
    ("arguments", "message"),
    [
        ({"wait": "yes"}, "wait must be a boolean"),
        ({"timeout": True}, "timeout must be greater than 0"),
        ({"timeout": 0}, "timeout must be greater than 0"),
        ({"timeout": 3601}, "timeout must be greater than 0"),
        ({"timeout": "soon"}, "timeout must be greater than 0"),
    ],
)
async def test_status_rejects_invalid_wait_arguments(runtime, arguments, message) -> None:
    with pytest.raises(ValidationError, match=message):
        await runtime.planner.status("plan_abc123", **arguments)


async def test_result_plan_requires_entry_reference(runtime) -> None:
    with pytest.raises(ValidationError, match="Entry reference is required"):
        await runtime.planner.plan(None, "results", {})


@pytest.mark.parametrize(
    ("table_id", "records", "expected"),
    [
        ("tbl_wrong", [record()], "explicit V2 result table ID"),
        (None, [record()], "explicit V2 result table ID"),
        (TABLE, [], "non-empty assay_results"),
        (TABLE, "records", "non-empty assay_results"),
        (TABLE, [record()] * 501, "at most 500"),
        (TABLE, ["bad"], "schemaId and fields"),
        (TABLE, [{"schemaId": "assaysch_abc123"}], "schemaId and fields"),
        (TABLE, [{"fields": {"ct": {"value": 1}}}], "schemaId and fields"),
        (TABLE, [record(fields={})], "required schema field"),
        (TABLE, [record(fields={"note": {"value": "only"}})], "required schema field"),
        (TABLE, [record(projectId="")], "non-empty string"),
        (TABLE, [record(projectId=5)], "non-empty string"),
        (TABLE, [record(schemaId="assaysch_other")], "does not match the selected table"),
        (TABLE, [record(fields={"ct": {"value": "high"}})], "wrong type"),
    ],
)
async def test_invalid_result_plans_are_rejected(
    runtime, fake, table_id, records, expected
) -> None:
    with pytest.raises(ValidationError, match=expected):
        await runtime.planner.plan(
            ENTRY_ID, "results", {"table_id": table_id, "assay_results": records}
        )
    assert fake.writes() == []


async def test_client_supplied_result_ids_are_rejected_with_the_row_index(runtime) -> None:
    with pytest.raises(ValidationError, match="server-assigned") as caught:
        await plan(runtime, [record(id="result_mine")])
    assert caught.value.details["row_problems"][0]["row_index"] == 0


async def test_result_plan_validates_dropdown_and_link_values_without_writes(runtime, fake) -> None:
    planned = await plan(
        runtime,
        [
            record(
                fields={
                    "ct": {"value": 12},
                    "status": {"value": "sfso_open"},
                    "sample": {"value": "ent_target"},
                }
            )
        ],
    )
    assert planned.after["validation_summary"] == {
        "rows_checked": 1,
        "rows_with_problems": 0,
        "counts_by_reason": {},
    }
    link_calls = [call for call in fake.calls if call.path.endswith("/custom-entity/ent_target")]
    assert len(link_calls) == 1
    assert fake.writes() == []


async def test_result_plan_reports_all_bad_rows_by_reason(runtime, fake) -> None:
    with pytest.raises(ValidationError, match="invalid rows") as caught:
        await plan(
            runtime,
            [
                record(fields={"ct": {"value": 12}, "status": {"value": "not-an-option"}}),
                record(fields={"ct": {"value": 12}, "sample": {"value": "ent_wrong_schema"}}),
                record(fields={"ct": {"value": 12}, "sample": {"value": "ent_wrong_schema"}}),
            ],
        )
    assert [
        (problem["row_index"], problem["reason"])
        for problem in caught.value.details["row_problems"]
    ] == [
        (0, "invalid_dropdown_value"),
        (1, "invalid_link_target"),
        (2, "invalid_link_target"),
    ]
    assert caught.value.details["validation_summary"] == {
        "rows_checked": 3,
        "rows_with_problems": 3,
        "counts_by_reason": {"invalid_dropdown_value": 1, "invalid_link_target": 2},
    }
    link_calls = [
        call for call in fake.calls if call.path.endswith("/custom-entity/ent_wrong_schema")
    ]
    assert len(link_calls) == 1
    assert fake.writes() == []


async def test_result_validation_caches_link_reads_and_indexes_rows_across_batches(
    runtime, fake
) -> None:
    fake.field_defs["assaysch_other"] = fake.field_defs["assaysch_abc123"]
    fake.extra_result_tables.append(
        {
            "type": "results_table",
            "apiId": "strtbl_other",
            "name": "Other results",
            "assayResultSchemaId": "assaysch_other",
            "columns": ["ct"],
        }
    )
    with pytest.raises(ValidationError) as caught:
        await runtime.planner.plan(
            ENTRY_ID,
            "results",
            {
                "batches": [
                    {
                        "table_id": TABLE,
                        "assay_results": [
                            record(fields={"ct": {"value": 12}, "sample": {"value": "ent_missing"}})
                        ],
                    },
                    {
                        "table_id": "strtbl_other",
                        "assay_results": [
                            record(
                                schemaId="assaysch_other",
                                fields={"ct": {"value": 12}, "sample": {"value": "ent_missing"}},
                            )
                        ],
                    },
                ]
            },
        )
    assert [problem["row_index"] for problem in caught.value.details["row_problems"]] == [0, 1]
    link_calls = [call for call in fake.calls if call.path.endswith("/custom-entity/ent_missing")]
    assert len(link_calls) == 1
    assert caught.value.details["validation_summary"]["rows_checked"] == 2
    assert fake.writes() == []


async def test_result_plan_rejects_link_field_without_allowed_schema(runtime, fake) -> None:
    sample_field = next(
        definition
        for definition in fake.field_defs["assaysch_abc123"]
        if definition["systemName"] == "sample"
    )
    sample_field.pop("linkDefinition")
    with pytest.raises(ValidationError) as caught:
        await plan(
            runtime,
            [record(fields={"ct": {"value": 12}, "sample": {"value": "ent_target"}})],
        )
    assert caught.value.details["row_problems"][0]["reason"] == "link_definition_unavailable"
    assert fake.writes() == []


async def test_table_must_be_on_the_selected_entry(runtime) -> None:
    with pytest.raises(NotFoundError):
        await plan(runtime, [record()], table_id="strtbl_elsewhere")


async def test_table_without_schema_is_unsupported(runtime, fake) -> None:
    fake.result_table_schema = None
    with pytest.raises(UnsupportedError, match="result schema"):
        await plan(runtime, [record()])


async def test_unknown_project_is_rejected(runtime) -> None:
    with pytest.raises(NotFoundError):
        await plan(runtime, [record(projectId="src_missing")])


async def test_plan_resolves_project_and_warns_when_unscoped(runtime) -> None:
    scoped = await plan(runtime, [record(projectId="src_abc123")])
    assert scoped.after["projects"] == {
        "src_abc123": {"id": "src_abc123", "name": "Synthetic project"}
    }
    assert not any("creator" in warning for warning in scoped.warnings)
    unscoped = await plan(runtime, [record()])
    assert any("creator" in warning for warning in unscoped.warnings)
    assert len(unscoped.after["client_record_ids"]) == 1
    assert unscoped.after["state"] == "pending_integration"


async def test_apply_posts_normalized_records_once(runtime, fake) -> None:
    planned = await plan(runtime, [record(fields={"assayfield_ct": {"value": 7}}), record()])
    applied = await runtime.planner.apply(planned.id)
    (payload,) = fake.bulk_payloads
    assert payload["tableId"] == TABLE
    assert [r["fields"] for r in payload["assayResults"]] == [
        {"ct": {"value": 7}},
        {"ct": {"value": 12}},
    ]
    assert applied["journal"]["result"]["http_status"] == 202
    assert applied["journal"]["result"]["task_id"] == "task_abc123"
    assert applied["journal"]["result"]["state"] == "submission_pending"
    stages = [event["stage"] for event in applied["journal"]["events"]]
    assert "create_results" in stages and "submit_results" not in stages
    assert applied["journal"]["result"]["record_ids"] == []
    assert applied["journal"]["result"]["client_record_ids"] == planned.after["client_record_ids"]
    assert all("id" not in record for record in payload["assayResults"])


async def test_multi_table_plan_previews_schemas_and_counts_and_applies_each_batch(
    runtime, fake
) -> None:
    planned = await multi_table_plan(runtime, fake)
    assert [
        (table["table_id"], table["schema_id"], table["row_count"])
        for table in planned.before["tables"]
    ] == [
        (TABLE, "assaysch_abc123", 1),
        ("strtbl_other", "assaysch_other", 1),
    ]
    fake.bulk_responses = [{"taskId": "task_first"}, {"taskId": "task_second"}]
    applied = await runtime.planner.apply(planned.id)
    assert [payload["tableId"] for payload in fake.bulk_payloads] == [TABLE, "strtbl_other"]
    assert [item["task_id"] for item in applied["journal"]["result"]["batches"]] == [
        "task_first",
        "task_second",
    ]
    assert "next_step" in applied
    assert "Assay results" in applied["next_step"]
    assert "Other results" in applied["next_step"]


async def test_multi_table_apply_keeps_success_when_another_table_fails(runtime, fake) -> None:
    planned = await multi_table_plan(runtime, fake)
    fake.fail("POST", "/assay-results:bulk-create", 400)
    with pytest.raises(PartialCompletionError) as caught:
        await runtime.planner.apply(planned.id)
    journal = caught.value.details["journal"]
    assert journal["status"] == "partial"
    submissions = [event for event in journal["events"] if event["stage"] == "create_results"]
    assert [event["status"] for event in submissions] == ["failed", "succeeded"]
    assert submissions[0]["details"]["table_id"] == TABLE
    assert submissions[1]["details"]["task_id"] == "task_abc123"
    assert submissions[1]["details"]["table_id"] == "strtbl_other"
    assert "Other results" in caught.value.details["next_step"]
    assert "Assay results" not in caught.value.details["next_step"]
    assert len(fake.bulk_payloads) == 1
    assert fake.bulk_payloads[0]["tableId"] == "strtbl_other"


async def test_status_waits_for_every_table_and_maps_documented_error_indices(
    runtime, fake
) -> None:
    planned = await multi_table_plan(runtime, fake)
    fake.bulk_responses = [{"taskId": "task_first"}, {"taskId": "task_second"}]
    await runtime.planner.apply(planned.id)
    fake.task_responses["task_first"] = [
        {"status": "RUNNING"},
        {"status": "FAILED", "errors": {"index": 0, "message": "invalid value"}},
    ]
    fake.tasks["task_second"] = {
        "status": "FAILED",
        "errors": [
            {"index": 0, "message": "invalid schema"},
            {"index": 7, "message": "out of range"},
            {"message": "table-level error"},
        ],
    }
    status = await runtime.planner.status(planned.id, wait=True, timeout=1)
    assert fake.task_requests.count("task_first") == 2
    assert fake.task_requests.count("task_second") == 1
    assert [
        (error["table_id"], error["row_index"], error["plan_row_index"])
        for error in status["row_errors"]
    ] == [
        (TABLE, 0, 0),
        ("strtbl_other", 0, 1),
    ]
    assert status["row_errors"][0]["error"]["message"] == "invalid value"
    assert status["row_errors"][1]["error"]["message"] == "invalid schema"
    assert status["unmatched_row_errors"] == [
        {
            "error": {"index": 7, "message": "out of range"},
            "reason": "Task error has no usable input row index",
            "table_id": "strtbl_other",
        },
        {
            "error": {"message": "table-level error"},
            "reason": "Task error has no usable input row index",
            "table_id": "strtbl_other",
        },
    ]


async def test_status_reports_task_that_is_still_running_after_timeout(runtime, fake) -> None:
    planned = await plan(runtime, [record()])
    await runtime.planner.apply(planned.id)
    fake.task = {"status": "RUNNING"}
    status = await runtime.planner.status(planned.id, wait=True, timeout=0.1)
    assert status["result_state"] == "submission_running"
    assert status["tasks"][0]["timed_out"] is True


async def test_status_timeout_bounds_a_task_request(runtime, fake, monkeypatch) -> None:
    planned = await plan(runtime, [record()])
    await runtime.planner.apply(planned.id)

    async def slow_task(_task_id):
        await asyncio.sleep(1)
        return {"status": "RUNNING"}

    monkeypatch.setattr(runtime.service, "get_task", slow_task)
    status = await runtime.planner.status(planned.id, wait=True, timeout=0.1)
    assert status["tasks"][0]["timed_out"] is True
    assert status["tasks"][0]["result_state"] == "submission_running"


async def test_status_persists_server_assigned_ids_not_local_correlations(runtime, fake) -> None:
    planned = await plan(runtime, [record()])
    await runtime.planner.apply(planned.id)
    fake.task = {
        "status": "SUCCEEDED",
        "response": {"assayResults": [{"id": "server-uuid", "v3Id": "result_server"}]},
    }
    status = await runtime.planner.status(planned.id)
    assert status["record_ids"] == ["server-uuid"]
    stored = runtime.repository.load_journal(planned.id).result
    assert stored["record_v3_ids"] == ["result_server"]
    assert stored["client_record_ids"] == planned.after["client_record_ids"]
    assert stored["state"] == "pending_integration"
    assert len(fake.bulk_payloads) == 1


async def test_apply_detects_result_schema_drift(runtime, fake) -> None:
    planned = await plan(runtime, [record()])
    fake.field_defs["assaysch_abc123"][1]["isRequired"] = True
    with pytest.raises(ConflictError, match="Result schema fields"):
        await runtime.planner.apply(planned.id)
    assert fake.writes() == []


async def test_apply_detects_table_reassigned_to_another_schema(runtime, fake) -> None:
    planned = await plan(runtime, [record()])
    fake.field_defs["assaysch_other"] = fake.field_defs["assaysch_abc123"]
    fake.result_table_schema = "assaysch_other"
    with pytest.raises(ConflictError, match="Result table schema"):
        await runtime.planner.apply(planned.id)
    assert fake.writes() == []


async def test_accepted_upload_without_task_id_reports_unknown_completion(runtime, fake) -> None:
    planned = await plan(runtime, [record()])
    fake.bulk_response = {}
    with pytest.raises(PartialCompletionError) as caught:
        await runtime.planner.apply(planned.id)
    assert caught.value.details["client_record_ids"] == planned.after["client_record_ids"]
    assert "pending" in caught.value.details["next_step"]
    assert (await runtime.planner.status(planned.id))["journal"]["status"] == "unknown"


@pytest.mark.parametrize(
    ("remote", "state"),
    [
        ({"status": "RUNNING"}, "submission_running"),
        ({"status": "FAILED"}, "submission_failed"),
        ({"status": "SUCCEEDED", "response": {"assayResults": []}}, "pending_integration"),
        ({"status": "SOMETHING_NEW"}, "unknown"),
    ],
)
async def test_status_maps_remote_task_state(runtime, fake, remote, state) -> None:
    planned = await plan(runtime, [record()])
    await runtime.planner.apply(planned.id)
    fake.task = remote
    status = await runtime.planner.status(planned.id)
    assert status["result_state"] == state
    assert status["journal"]["result"]["state"] == state


async def test_status_surfaces_task_lookup_errors_without_losing_the_journal(runtime, fake) -> None:
    planned = await plan(runtime, [record()])
    await runtime.planner.apply(planned.id)
    fake.fail("GET", "/tasks/", 403)
    status = await runtime.planner.status(planned.id)
    assert status["remote_task_error"]["code"] == "access_denied"
    assert status["journal"]["status"] == "succeeded"
    assert status["journal"]["result"]["state"] == "submission_pending"


async def test_apply_tells_the_user_to_accept_the_pending_results(runtime, fake) -> None:
    planned = await plan(runtime, [record()])
    applied = await runtime.planner.apply(planned.id)
    step = applied["next_step"]
    assert "pending" in step
    assert ENTRY_ID in step
    assert "plug" in step
    assert "Assay results" in step


async def test_plan_warning_names_the_acceptance_step(runtime) -> None:
    planned = await plan(runtime, [record()])
    assert any("plug icon" in warning for warning in planned.warnings)


async def test_non_results_apply_has_no_next_step(runtime) -> None:
    planned = await runtime.planner.plan(
        ENTRY_ID,
        "table",
        {"operation": "append_rows", "table_part_id": "docpart_table1", "rows": [["D", "4"]]},
    )
    applied = await runtime.planner.apply(planned.id)
    assert "next_step" not in applied


async def test_results_apply_returns_web_url_and_links_it_in_next_step(runtime) -> None:
    planned = await plan(runtime, [record()])
    url = entry_web_url(ENTRY_ID)
    assert planned.before["tables"][0]["table"]["entry_web_url"] == url
    applied = await runtime.planner.apply(planned.id)
    assert applied["web_url"] == url
    assert applied["journal"]["result"]["web_url"] == url
    assert f"Open entry {ENTRY_ID} at {url}," in applied["next_step"]


async def test_results_next_step_falls_back_to_the_id_without_web_url(runtime, fake) -> None:
    fake.omit_entry_web_url = True
    planned = await plan(runtime, [record()])
    applied = await runtime.planner.apply(planned.id)
    assert applied["web_url"] is None
    assert f"Open entry {ENTRY_ID}, go to" in applied["next_step"]


async def test_non_entry_apply_has_no_web_url(runtime) -> None:
    planned = await runtime.planner.plan(
        ENTRY_ID,
        "table",
        {"operation": "append_rows", "table_part_id": "docpart_table1", "rows": [["D", "4"]]},
    )
    applied = await runtime.planner.apply(planned.id)
    assert "web_url" not in applied
