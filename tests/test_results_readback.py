"""Read back result uploads, including pending rows, and diff them against the plan."""

from __future__ import annotations

import json
import shutil

import pytest
from conftest import runtime_for
from fake_benchling import ENTRY_ID, entry_web_url
from typer.testing import CliRunner

from benchling_tools import cli
from benchling_tools.config import ConfigStore
from benchling_tools.errors import ValidationError
from benchling_tools.models import PlanStatus
from benchling_tools.plans import readback
from benchling_tools.plans.repository import checksum_for

TABLE = "strtbl_example"


def record(ct=12, **overrides):
    return {"schemaId": "assaysch_abc123", "fields": {"ct": {"value": ct}}, **overrides}


async def applied_plan(runtime, fake, records):
    planned = await runtime.planner.plan(
        ENTRY_ID, "results", {"table_id": TABLE, "assay_results": records}
    )
    await runtime.planner.apply(planned.id)
    return planned


def journal_text(runtime, plan_id):
    return (runtime.repository.config.journals_dir / f"{plan_id}.json").read_text()


def bulk_get_calls(fake):
    return [call for call in fake.calls if call.path == "/api/v2/assay-results:bulk-get"]


async def test_pending_by_plan_reads_task_ids_and_writes_nothing(runtime, fake) -> None:
    planned = await applied_plan(runtime, fake, [record(1), record(2)])
    ids = fake.complete_upload()
    before = journal_text(runtime, planned.id)
    writes = len(fake.writes())

    data = await runtime.planner.read_back_results(plan_id=planned.id)

    assert data["source"] == {
        "mode": "plan",
        "plan_id": planned.id,
        "entry_id": ENTRY_ID,
        "web_url": entry_web_url(ENTRY_ID),
    }
    assert data["counts"] == {
        "read": 2,
        "pending": 2,
        "accepted": 0,
        "archived": 0,
        "not_found": 0,
    }
    assert [item["id"] for item in data["items"]] == ids
    assert all(item["needs_accept"] and item["table_id"] == TABLE for item in data["items"])
    assert data["needs_accept"] == ids
    assert data["batches"][0]["task_status"] == "SUCCEEDED"
    assert (
        "review and accept them in Benchling" in data["next_step"]
        and "plug" not in data["next_step"]
        and '"Assay results"' in data["next_step"]
    )
    assert f"entry {ENTRY_ID} at {entry_web_url(ENTRY_ID)}," in data["next_step"]
    assert journal_text(runtime, planned.id) == before
    assert len(fake.writes()) == writes
    (call,) = bulk_get_calls(fake)
    assert call.params == {"assayResultIds": ",".join(ids)}


async def test_pending_by_plan_uses_ids_stored_by_changes_status(runtime, fake) -> None:
    planned = await applied_plan(runtime, fake, [record()])
    fake.complete_upload()
    await runtime.planner.status(planned.id)
    fake.task_requests.clear()

    data = await runtime.planner.read_back_results(plan_id=planned.id)

    assert fake.task_requests == []
    assert data["counts"]["pending"] == 1


async def test_pending_and_verify_load_portable_plan_from_fresh_home(
    runtime, fake, tmp_path
) -> None:
    planned = await applied_plan(runtime, fake, [record(1), record(2)])
    fake.complete_upload()
    portable_dir = tmp_path / "portable"
    portable_dir.mkdir()
    plan_file = portable_dir / "results-plan.json"
    shutil.copyfile(runtime.repository.config.plans_dir / f"{planned.id}.json", plan_file)
    shutil.copyfile(
        runtime.repository.config.journals_dir / f"{planned.id}.json",
        portable_dir / f"{planned.id}.journal.json",
    )
    runtime.repository.config = ConfigStore(tmp_path / "fresh-home")

    pending = await runtime.planner.read_back_results(plan_id=str(plan_file))
    verified = await runtime.planner.verify_results(str(plan_file))

    assert pending["counts"]["pending"] == 2
    assert pending["source"]["plan_id"] == planned.id
    assert verified["ok"] is True and verified["counts"]["matched"] == 2


async def test_readback_accepts_legacy_submit_results_journal_stage(runtime, fake) -> None:
    planned = await applied_plan(runtime, fake, [record()])
    fake.complete_upload()
    journal = runtime.repository.load_journal(planned.id)
    for event in journal.events:
        if event.stage == "create_results":
            event.stage = "submit_results"
    runtime.repository.save_journal(journal)

    status = await runtime.planner.status(planned.id)
    pending = await runtime.planner.read_back_results(plan_id=planned.id)
    verified = await runtime.planner.verify_results(planned.id)

    assert status["tasks"][0]["task_id"] == "task_abc123"
    assert pending["counts"]["pending"] == 1
    assert verified["ok"] is True and verified["counts"]["matched"] == 1


async def test_accepted_rows_are_not_reported_as_needing_accept(runtime, fake) -> None:
    planned = await applied_plan(runtime, fake, [record(1), record(2)])
    first, second = fake.complete_upload()
    fake.accept_results([first])

    data = await runtime.planner.read_back_results(plan_id=planned.id, limit=1)

    assert data["counts"]["pending"] == 1 and data["counts"]["accepted"] == 1
    assert data["items"][0]["id"] == second
    assert data["returned"] == 1 and data["truncated"] is True
    assert data["needs_accept"] == [second]
    assert data["next_step"].startswith("1 result is pending")
    fake.accept_results([second])
    data = await runtime.planner.read_back_results(plan_id=planned.id)
    assert data["counts"]["pending"] == 0 and data["next_step"] is None


def rewrite_plan(runtime, plan_id, **changes):
    """Edit a stored plan and re-checksum it, as if it had been planned that way."""
    path = runtime.repository.config.plans_dir / f"{plan_id}.json"
    plan = json.loads(path.read_text())
    for key, value in changes.items():
        if key == "operation":
            plan["preconditions"]["operation"] = value
        else:
            plan[key] = value
    plan["checksum"] = checksum_for(plan)
    path.write_text(json.dumps(plan))


async def test_pending_by_entry_reads_every_applied_upload_plan_newest_first(runtime, fake) -> None:
    fake.bulk_responses = [{"taskId": "task_first"}, {"taskId": "task_second"}]
    first_plan = await applied_plan(runtime, fake, [record(1)])
    second_plan = await applied_plan(runtime, fake, [record(2), record(3)])
    (first,) = fake.complete_upload("task_first", payload_index=0)
    second_ids = fake.complete_upload("task_second", payload_index=1)
    fake.accept_results([first])
    writes = len(fake.writes())

    for reference in (ENTRY_ID, "EXP-1", entry_web_url(ENTRY_ID)):
        data = await runtime.planner.read_back_results(entry=reference)

        assert data["source"] == {
            "mode": "entry",
            "entry_id": ENTRY_ID,
            "display_id": "EXP-1",
            "web_url": entry_web_url(ENTRY_ID),
        }
        assert [plan["plan_id"] for plan in data["plans"]] == [second_plan.id, first_plan.id]
        assert [plan["record_count"] for plan in data["plans"]] == [2, 1]
        assert all(plan["applied_at"] for plan in data["plans"])
        assert data["counts"] == {
            "read": 3,
            "pending": 2,
            "accepted": 1,
            "archived": 0,
            "not_found": 0,
        }
        assert data["needs_accept"] == second_ids
        assert [item["state"] for item in data["items"]] == ["pending", "pending", "accepted"]
        assert all(item["v2_id"] and item["v3_id"] for item in data["items"])
        assert all(item["table_id"] == TABLE for item in data["items"])
        assert '"Assay results"' in data["next_step"]
    assert len(fake.writes()) == writes


async def test_pending_by_entry_skips_updates_other_entries_and_unapplied_plans(
    runtime, fake
) -> None:
    fake.bulk_responses = [
        {"taskId": "task_kept"},
        {"taskId": "task_update"},
        {"taskId": "task_failed"},
        {"taskId": "task_other"},
    ]
    kept = await applied_plan(runtime, fake, [record(1)])
    update = await applied_plan(runtime, fake, [record(2)])
    failed = await applied_plan(runtime, fake, [record(3)])
    other = await applied_plan(runtime, fake, [record(4)])
    await runtime.planner.plan(
        ENTRY_ID, "results", {"table_id": TABLE, "assay_results": [record()]}
    )
    (kept_id,) = fake.complete_upload("task_kept", payload_index=0)
    for index, task in enumerate(("task_update", "task_failed", "task_other"), start=1):
        fake.complete_upload(task, payload_index=index)
    rewrite_plan(runtime, update.id, operation="update")
    rewrite_plan(runtime, other.id, entry_id="etr_other123")
    journal = runtime.repository.load_journal(failed.id)
    journal.status = PlanStatus.FAILED
    runtime.repository.save_journal(journal)
    (runtime.repository.config.plans_dir / "notes.json").write_text("{}")
    (runtime.repository.config.plans_dir / f"{'f' * 32}.json").write_text("not json")

    data = await runtime.planner.read_back_results(entry=ENTRY_ID)

    assert [plan["plan_id"] for plan in data["plans"]] == [kept.id]
    assert data["needs_accept"] == [kept_id]


async def test_pending_by_entry_without_plans_explains_what_this_home_knows(
    runtime, fake, tmp_path
) -> None:
    runtime.repository.config = ConfigStore(tmp_path / "empty-home")
    data = await runtime.planner.read_back_results(entry=ENTRY_ID)

    assert data["plans"] == [] and data["items"] == []
    assert data["counts"]["read"] == 0
    assert "BENCHLING_TOOLS_HOME" in data["next_step"]
    assert "results pending --plan BUNDLE.json" in data["next_step"]
    assert "--schema SCHEMA_ID --created-after TIME" in data["next_step"]
    assert bulk_get_calls(fake) == []


async def test_pending_by_ids_chunks_bulk_get_at_200_and_lists_not_found(runtime, fake) -> None:
    fake.assay_results = [
        {"id": f"id{n}", "entryId": None, "schema": {"id": "assaysch_abc123"}, "fields": {}}
        for n in range(250)
    ]
    requested = [f"id{n}" for n in range(250)] + ["id0", "missing"]

    data = await runtime.planner.read_back_results(result_ids=requested, limit=5)

    assert [len(call.params["assayResultIds"].split(",")) for call in bulk_get_calls(fake)] == [
        200,
        51,
    ]
    assert data["counts"]["read"] == 250 and data["counts"]["pending"] == 250
    assert data["not_found"] == ["missing"]
    assert len(data["items"]) == 5 and len(data["needs_accept"]) == 5
    assert data["source"] == {"mode": "ids", "requested": 251}
    assert "the entry the results were uploaded to" in data["next_step"]


async def test_pending_by_schema_and_time_uses_v2_list_without_an_entry_filter(
    runtime, fake
) -> None:
    fake.assay_results = [
        {
            "id": f"id{n}",
            "entryId": ENTRY_ID if n == 1 else None,
            "createdAt": f"2026-11-1{n}T00:00:00Z",
            "schema": {"id": "assaysch_abc123"},
            "fields": {},
        }
        for n in range(1, 5)
    ] + [
        {
            "id": "other",
            "entryId": None,
            "createdAt": "2026-11-19T00:00:00Z",
            "schema": {"id": "assaysch_two"},
        }
    ]
    fake.assay_result_page_size = 2

    first = await runtime.planner.read_back_results(
        schema_id="assaysch_abc123", created_after="2026-11-12T00:00:00Z", limit=2
    )
    assert [item["id"] for item in first["items"]] == ["id2", "id3"]
    assert first["next_token"]
    second = await runtime.planner.read_back_results(
        schema_id="assaysch_abc123",
        created_after="2026-11-12T00:00:00Z",
        limit=2,
        cursor=first["next_token"],
    )
    assert [item["id"] for item in second["items"]] == ["id4"]
    assert second["next_token"] is None
    lists = [call for call in fake.calls if call.path == "/api/v2/assay-results"]
    assert lists[0].params == {
        "schemaId": "assaysch_abc123",
        "createdAt.gte": "2026-11-12T00:00:00Z",
        "sort": "createdAt:asc",
        "pageSize": "2",
    }
    assert all("entry" not in key.lower() for call in lists for key in call.params)
    assert fake.writes() == []


@pytest.mark.parametrize(
    ("arguments", "message"),
    [
        ({}, "exactly one"),
        ({"plan_id": "abc", "result_ids": ["x"]}, "exactly one"),
        ({"entry": ENTRY_ID, "plan_id": "abc"}, "exactly one"),
        ({"entry": ENTRY_ID, "result_ids": ["x"]}, "exactly one"),
        ({"entry": ENTRY_ID, "schema_id": "assaysch_abc123"}, "exactly one"),
        ({"entry": ENTRY_ID, "cursor": "1"}, "cursor applies only"),
        ({"entry": " "}, "entry must be"),
        ({"schema_id": "assaysch_abc123"}, "passed together"),
        ({"created_after": "2026-11-19"}, "passed together"),
        ({"result_ids": ["x"], "cursor": "1"}, "cursor applies only"),
        ({"result_ids": ["x"], "limit": 0}, "limit must be between 1 and 100"),
        ({"result_ids": ["x"], "limit": True}, "limit must be between 1 and 100"),
        ({"result_ids": []}, "non-empty list"),
        ({"result_ids": ["a,b"]}, "commas"),
        ({"schema_id": "assaysch_abc123", "created_after": "yesterday"}, "RFC 3339"),
    ],
)
async def test_read_back_rejects_invalid_arguments(runtime, fake, arguments, message) -> None:
    with pytest.raises(ValidationError, match=message):
        await runtime.planner.read_back_results(**arguments)
    assert [call for call in fake.calls if "/assay-results" in call.path] == []


async def test_read_back_requires_an_applied_result_upload_plan(runtime, fake) -> None:
    unapplied = await runtime.planner.plan(
        ENTRY_ID, "results", {"table_id": TABLE, "assay_results": [record()]}
    )
    with pytest.raises(ValidationError, match="not been applied"):
        await runtime.planner.verify_results(unapplied.id)
    table_plan = await runtime.planner.plan(
        ENTRY_ID,
        "table",
        {"operation": "append_rows", "table_part_id": "docpart_table1", "rows": [["D", "4"]]},
    )
    with pytest.raises(ValidationError, match="Only result upload plans"):
        await runtime.planner.read_back_results(plan_id=table_plan.id)
    with pytest.raises(ValidationError, match="Unknown change plan"):
        await runtime.planner.verify_results("0" * 32)


async def test_verify_matches_every_planned_row_regardless_of_order(runtime, fake) -> None:
    planned = await applied_plan(runtime, fake, [record(1), record(2)])
    ids = fake.complete_upload()
    fake.assay_results.reverse()
    fake.assay_results[0]["fields"]["ct"]["value"] = 2.0
    fake.assay_results[1]["fields"]["ct"]["value"] = 1.0
    fake.tasks["task_abc123"]["response"]["assayResults"].reverse()

    data = await runtime.planner.verify_results(planned.id)

    assert data["ok"] is True
    assert data["counts"]["matched"] == 2 and data["counts"]["pending"] == 2
    assert data["mismatched"] == data["missing"] == data["extra"] == []
    assert [row["plan_row_index"] for row in data["needs_accept"]] == [0, 1]
    assert {row["result_id"] for row in data["needs_accept"]} == set(ids)
    assert data["summary"].startswith("2 of 2 planned rows match")
    assert "review and accept them in Benchling" in data["next_step"]
    assert fake.writes()[-1].path == "/api/v2/assay-results:bulk-create"


async def test_verify_reports_mismatched_fields_by_system_name(runtime, fake) -> None:
    planned = await applied_plan(runtime, fake, [record(1), record(2)])
    _first, second = fake.complete_upload()
    fake.assay_results[1]["fields"]["ct"]["value"] = 20
    fake.accept_results([second])

    data = await runtime.planner.verify_results(planned.id)

    assert data["ok"] is False
    assert data["counts"]["matched"] == 1 and data["counts"]["mismatched"] == 1
    assert data["mismatched"] == [
        {
            "table_id": TABLE,
            "plan_row_index": 1,
            "result_id": second,
            "v2_id": second,
            "v3_id": "result_up2",
            "state": "accepted",
            "fields": [{"field": "ct", "planned": 2, "actual": 20}],
        }
    ]
    assert data["counts"]["accepted"] == 1 and data["counts"]["pending"] == 1
    assert len(data["needs_accept"]) == 1


async def test_verify_reports_extra_and_not_found_records(runtime, fake) -> None:
    planned = await applied_plan(runtime, fake, [record(1), record(2)])
    first, second = fake.complete_upload()
    fake.assay_results.append(
        {
            "id": "extra",
            "entryId": None,
            "schema": {"id": "assaysch_abc123"},
            "fields": {"ct": {"value": 9}},
        }
    )
    fake.assay_results = [item for item in fake.assay_results if item["id"] != second]
    fake.tasks["task_abc123"]["response"]["assayResults"].append({"id": "extra"})

    data = await runtime.planner.verify_results(planned.id)

    assert data["counts"]["not_found"] == 1
    assert data["counts"]["matched"] == 1
    assert data["mismatched"][0]["result_id"] == "extra"
    assert data["mismatched"][0]["plan_row_index"] == 1
    fake.assay_results.append(
        {
            "id": second,
            "entryId": None,
            "schema": {"id": "assaysch_abc123"},
            "fields": {"ct": {"value": 2}},
        }
    )
    data = await runtime.planner.verify_results(planned.id)
    assert data["counts"]["matched"] == 2
    assert data["extra"] == [
        {
            "table_id": TABLE,
            "result_id": "extra",
            "v2_id": "extra",
            "v3_id": None,
            "state": "pending",
        }
    ]
    assert first in {row["result_id"] for row in data["needs_accept"]}


async def test_verify_reports_missing_rows_while_the_task_runs(runtime, fake) -> None:
    planned = await applied_plan(runtime, fake, [record(1), record(2)])
    fake.task = {"status": "RUNNING"}

    data = await runtime.planner.verify_results(planned.id)

    assert data["ok"] is False
    assert data["counts"]["missing"] == 2 and data["counts"]["fetched"] == 0
    assert {row["reason"] for row in data["missing"]} == {"task_running"}
    assert data["batches"][0]["task_status"] == "RUNNING"
    assert data["next_step"] is None
    assert bulk_get_calls(fake) == []


async def test_verify_names_a_failed_table_upload_in_a_multi_table_plan(runtime, fake) -> None:
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
    planned = await runtime.planner.plan(
        ENTRY_ID,
        "results",
        {
            "batches": [
                {"table_id": TABLE, "assay_results": [record(1)]},
                {
                    "table_id": "strtbl_other",
                    "assay_results": [record(8, schemaId="assaysch_other")],
                },
            ]
        },
    )
    fake.fail("POST", "/assay-results:bulk-create", 400)
    with pytest.raises(Exception):  # noqa: B017 - partial completion is covered elsewhere
        await runtime.planner.apply(planned.id)
    fake.complete_upload()

    data = await runtime.planner.verify_results(planned.id)

    assert data["missing"] == [
        {"table_id": TABLE, "plan_row_index": 0, "reason": "submission_failed"}
    ]
    assert data["counts"]["matched"] == 1
    assert data["batches"][0]["submission"] == "submission_failed"
    assert '"Other results"' in data["next_step"]
    assert '"Assay results"' not in data["next_step"]


async def test_verify_bounds_each_sample_but_counts_every_row(runtime, fake) -> None:
    planned = await applied_plan(runtime, fake, [record(n) for n in range(30)])
    fake.complete_upload()
    for item in fake.assay_results:
        item["fields"]["ct"]["value"] = -1

    data = await runtime.planner.verify_results(planned.id, sample=5)

    assert data["counts"]["mismatched"] == 30
    assert len(data["mismatched"]) == 5 and len(data["needs_accept"]) == 5
    assert data["sample_limit"] == 5
    for bad in (0, 201, True):
        with pytest.raises(ValidationError, match="sample"):
            await runtime.planner.verify_results(planned.id, sample=bad)


@pytest.mark.parametrize(
    ("planned", "actual", "equal"),
    [
        (12, 12.0, True),
        (True, 1, False),
        (["b", "a"], ["a", "b"], True),
        ([1, 2], [2, 1], False),
        (["a"], ["a", "b"], False),
        ("x", "x", True),
        (None, None, True),
    ],
)
def test_values_equal(planned, actual, equal) -> None:
    assert readback.values_equal(planned, actual) is equal


def test_missing_planned_field_is_a_mismatch_unless_planned_null() -> None:
    fetched = {"schema": {"id": "assaysch_abc123"}, "fields": {"ct": {"value": 1}}}
    assert readback.field_mismatches({"note": {"value": None}}, fetched, "assaysch_abc123") == []
    assert readback.field_mismatches({"note": {"value": "n"}}, fetched, "assaysch_abc123") == [
        {"field": "note", "planned": "n", "actual": None, "present": False}
    ]
    assert readback.field_mismatches({}, fetched, "assaysch_other")[0]["field"] == "schemaId"


def test_archived_results_are_not_pending() -> None:
    assert readback.result_state({"entryId": None, "archiveRecord": {"reason": "Retired"}}) == (
        "archived"
    )


@pytest.fixture
def invoke(benchling_env, fake, monkeypatch):
    monkeypatch.setattr(cli, "Runtime", lambda environment: runtime_for(fake, environment))

    def run(*args: str):
        result = CliRunner().invoke(cli.app, [*args, "--env", "sandbox"])
        return result.exit_code, json.loads(result.stdout)

    return run


def test_cli_pending_and_verify_read_back_a_plan(invoke, fake, tmp_path) -> None:
    spec = tmp_path / "results.json"
    spec.write_text(json.dumps({"table_id": TABLE, "assay_results": [record()]}))
    code, planned = invoke(
        "changes", "plan", "--entry", ENTRY_ID, "--family", "results", "--spec", str(spec)
    )
    assert code == 0, planned
    plan_id = planned["data"]["id"]
    code, applied = invoke("changes", "apply", plan_id)
    assert code == 0, applied
    (result_id,) = fake.complete_upload()

    code, pending = invoke("results", "pending", "--plan", plan_id)
    assert code == 0, pending
    assert pending["data"]["needs_accept"] == [result_id]
    code, by_id = invoke("results", "pending", "--id", result_id, "--limit", "1")
    assert code == 0 and by_id["data"]["counts"]["pending"] == 1
    code, verified = invoke("results", "verify", plan_id, "--sample", "3")
    assert code == 0, verified
    assert verified["data"]["ok"] is True
    assert verified["data"]["sample_limit"] == 3
    code, failed = invoke("results", "pending")
    assert code == 2 and failed["error"]["code"] == "validation_error"
    code, by_entry = invoke("results", "pending", "--entry", "EXP-1")
    assert code == 0, by_entry
    assert [plan["plan_id"] for plan in by_entry["data"]["plans"]] == [plan_id]
    assert by_entry["data"]["needs_accept"] == [result_id]
    code, failed = invoke("results", "pending", "--entry", ENTRY_ID, "--plan", plan_id)
    assert code == 2 and failed["error"]["code"] == "validation_error"
    assert "exactly one of entry" in failed["error"]["message"]


def test_cli_pending_and_verify_accept_portable_plan_file(
    invoke, fake, monkeypatch, tmp_path
) -> None:
    spec = tmp_path / "portable-results.json"
    spec.write_text(json.dumps({"table_id": TABLE, "assay_results": [record(1), record(2)]}))
    code, planned = invoke(
        "changes", "plan", "--entry", ENTRY_ID, "--family", "results", "--spec", str(spec)
    )
    assert code == 0, planned
    plan_id = planned["data"]["id"]
    code, applied = invoke("changes", "apply", plan_id)
    assert code == 0, applied
    fake.complete_upload()
    portable_dir = tmp_path / "portable-cli"
    portable_dir.mkdir()
    bundle_file = portable_dir / "portable-plan.json"
    code, exported = invoke("changes", "export", plan_id, "--output", str(bundle_file))
    assert code == 0, exported
    content = bundle_file.read_text()
    bundle = json.loads(content)
    assert "client_secret" not in content and "access_token" not in content
    assert str(ConfigStore().root) not in content
    assert bundle["checksum"] == bundle["plan"]["checksum"]
    assert bundle["journal"]["plan_id"] == plan_id
    fresh_home = tmp_path / "cli-home"
    runtime_factory = cli.Runtime

    def runtime_with_fresh_home(environment):
        active = runtime_factory(environment)
        active.repository.config = ConfigStore(fresh_home)
        return active

    monkeypatch.setattr(cli, "Runtime", runtime_with_fresh_home)

    status_code, status = invoke("changes", "status", str(bundle_file))
    pending_code, pending = invoke("results", "pending", "--plan", str(bundle_file))
    verify_code, verified = invoke("results", "verify", str(bundle_file))

    assert status_code == pending_code == verify_code == 0
    assert status["data"]["tasks"][0]["task_status"] == "SUCCEEDED"
    assert pending["data"]["counts"]["pending"] == 2
    assert verified["data"]["ok"] is True and verified["data"]["counts"]["matched"] == 2


def test_changes_export_refuses_to_overwrite_unless_forced(invoke, tmp_path) -> None:
    spec = tmp_path / "export-results.json"
    spec.write_text(json.dumps({"table_id": TABLE, "assay_results": [record()]}))
    code, planned = invoke(
        "changes", "plan", "--entry", ENTRY_ID, "--family", "results", "--spec", str(spec)
    )
    assert code == 0, planned
    output = tmp_path / "bundle.json"

    code, first = invoke("changes", "export", planned["data"]["id"], "--output", str(output))
    assert code == 0, first
    original = output.read_text()
    code, refused = invoke("changes", "export", planned["data"]["id"], "--output", str(output))
    assert code == 2 and "without --force" in refused["error"]["message"]
    assert output.read_text() == original
    code, forced = invoke(
        "changes",
        "export",
        planned["data"]["id"],
        "--output",
        str(output),
        "--force",
    )
    assert code == 0, forced


def test_tampered_plan_bundle_is_rejected(invoke, tmp_path) -> None:
    spec = tmp_path / "tampered-results.json"
    spec.write_text(json.dumps({"table_id": TABLE, "assay_results": [record()]}))
    code, planned = invoke(
        "changes", "plan", "--entry", ENTRY_ID, "--family", "results", "--spec", str(spec)
    )
    assert code == 0, planned
    output = tmp_path / "tampered-bundle.json"
    code, exported = invoke("changes", "export", planned["data"]["id"], "--output", str(output))
    assert code == 0, exported
    bundle = json.loads(output.read_text())
    bundle["plan"]["after"]["client_record_ids"] = ["tampered"]
    output.write_text(json.dumps(bundle))

    code, response = invoke("results", "verify", str(output))

    assert code == 2
    assert response["error"]["code"] == "validation_error"
    assert "checksum does not match" in response["error"]["message"]


def test_plan_file_without_journal_explains_how_to_export(invoke, tmp_path) -> None:
    spec = tmp_path / "missing-journal-results.json"
    spec.write_text(json.dumps({"table_id": TABLE, "assay_results": [record()]}))
    code, planned = invoke(
        "changes", "plan", "--entry", ENTRY_ID, "--family", "results", "--spec", str(spec)
    )
    assert code == 0, planned
    plan_file = tmp_path / "plan-without-journal.json"
    shutil.copyfile(ConfigStore().plans_dir / f"{planned['data']['id']}.json", plan_file)

    code, response = invoke("results", "pending", "--plan", str(plan_file))

    assert code == 2
    assert "journal is missing" in response["error"]["message"]
    assert "changes export" in response["error"]["message"]


async def test_plan_bundle_rejects_invalid_structure_and_checksums(runtime, tmp_path) -> None:
    planned = await runtime.planner.plan(
        ENTRY_ID, "results", {"table_id": TABLE, "assay_results": [record()]}
    )
    bundle_file = tmp_path / "bundle.json"
    plain_plan = tmp_path / "plain-plan.json"
    shutil.copyfile(runtime.repository.config.plans_dir / f"{planned.id}.json", plain_plan)
    assert runtime.repository.load_plan_file(plain_plan).id == planned.id
    runtime.repository.export_plan_bundle(planned.id, str(bundle_file))
    original = json.loads(bundle_file.read_text())

    invalid_documents = [
        [],
        {"bundle_version": "2.0"},
        {"bundle_version": "1.0", "plan": {}, "journal": {}},
    ]
    bad_bundle_checksum = json.loads(json.dumps(original))
    bad_bundle_checksum["checksum"] = "0" * 64
    invalid_documents.append(bad_bundle_checksum)
    bad_journal = json.loads(json.dumps(original))
    bad_journal["journal"]["plan_id"] = "0" * 32
    invalid_documents.append(bad_journal)
    bad_plan_id = json.loads(json.dumps(original))
    bad_plan_id["plan"]["id"] = "invalid"
    bad_plan_id["plan"]["checksum"] = checksum_for(bad_plan_id["plan"])
    bad_plan_id["checksum"] = bad_plan_id["plan"]["checksum"]
    invalid_documents.append(bad_plan_id)

    for document in invalid_documents:
        bundle_file.write_text(json.dumps(document))
        with pytest.raises(ValidationError):
            runtime.repository.load_plan_document(bundle_file)

    with pytest.raises(ValidationError, match="Unknown change plan file"):
        runtime.repository.load_plan_document(tmp_path / "missing.json")


async def test_plan_bundle_export_validates_destination_options(runtime, tmp_path) -> None:
    planned = await runtime.planner.plan(
        ENTRY_ID, "results", {"table_id": TABLE, "assay_results": [record()]}
    )
    with pytest.raises(ValidationError, match="Plan ID already exists"):
        runtime.repository.save_plan(planned)
    for output, force, message in (
        (" ", False, "non-empty path"),
        (str(tmp_path / "missing" / "bundle.json"), False, "directory does not exist"),
        (str(tmp_path), False, "not a file"),
        (str(tmp_path / "bundle.json"), 1, "force must be true or false"),
    ):
        with pytest.raises(ValidationError, match=message):
            runtime.repository.export_plan_bundle(planned.id, output, force=force)


async def test_task_lookup_failure_and_unconfirmed_uploads_are_reported(runtime, fake) -> None:
    planned = await applied_plan(runtime, fake, [record()])
    fake.fail("GET", "/tasks/", 403, times=2)
    pending = await runtime.planner.read_back_results(plan_id=planned.id)
    assert pending["batches"][0]["remote_task_error"]["code"] == "access_denied"
    assert pending["items"] == [] and bulk_get_calls(fake) == []
    verified = await runtime.planner.verify_results(planned.id)
    assert verified["missing"][0]["reason"] == "task_unreadable"

    unconfirmed = await runtime.planner.plan(
        ENTRY_ID, "results", {"table_id": TABLE, "assay_results": [record()]}
    )
    fake.bulk_response = {}
    with pytest.raises(Exception):  # noqa: B017 - the unknown outcome is covered elsewhere
        await runtime.planner.apply(unconfirmed.id)
    verified = await runtime.planner.verify_results(unconfirmed.id)
    assert verified["missing"][0]["reason"] == "submission_unconfirmed"
    assert verified["batches"][0]["submission"] == "submission_unconfirmed"


async def test_verify_reads_a_single_table_plan_without_batches(runtime, fake) -> None:
    planned = await applied_plan(runtime, fake, [record(4)])
    fake.complete_upload()
    stored = runtime.repository.load_plan(planned.id)
    legacy = stored.model_copy(
        update={"after": {k: v for k, v in stored.after.items() if k != "batches"}}
    )
    batches = readback.planned_batches(legacy)
    assert batches[0]["table_id"] == TABLE and batches[0]["table_name"] == "Assay results"
    assert readback.planned_batches(stored.model_copy(update={"after": {"batches": ["bad"]}})) == []


@pytest.mark.parametrize(
    ("payload", "message"),
    [
        ({"assayResults": "nope"}, "unexpected bulk result response"),
        ({"assayResults": ["nope"]}, "unexpected bulk result response"),
    ],
)
async def test_bulk_get_rejects_malformed_responses(runtime, fake, payload, message) -> None:
    fake.payload_overrides.append(("/assay-results:bulk-get", 200, payload))
    with pytest.raises(Exception, match=message):
        await runtime.service.get_assay_results(["id1"])


async def test_bulk_get_ignores_records_without_ids(runtime, fake) -> None:
    fake.payload_overrides.append(("/assay-results:bulk-get", 200, {"assayResults": [{}]}))
    assert await runtime.service.get_assay_results(["id1"]) == {
        "items": [],
        "not_found": ["id1"],
    }


@pytest.mark.parametrize(
    ("arguments", "message"),
    [
        ({"schema_id": " ", "created_after": "2026-11-19"}, "schema_id is required"),
        ({"schema_id": "assaysch_abc123", "created_after": ""}, "created_after is required"),
        (
            {"schema_id": "assaysch_abc123", "created_after": "2026-11-19", "page_size": 0},
            "page_size",
        ),
        (
            {"schema_id": "assaysch_abc123", "created_after": "2026-11-19", "page_size": True},
            "page_size",
        ),
    ],
)
async def test_list_assay_results_validates_arguments(runtime, fake, arguments, message) -> None:
    with pytest.raises(ValidationError, match=message):
        await runtime.service.list_assay_results(**arguments)
    assert fake.calls == []


async def test_list_assay_results_rejects_a_malformed_page(runtime, fake) -> None:
    fake.payload_overrides.append(("/assay-results", 200, {"items": []}))
    with pytest.raises(Exception, match="unexpected result list response"):
        await runtime.service.list_assay_results(
            schema_id="assaysch_abc123", created_after="2026-11-19"
        )


def test_readback_helpers_handle_sparse_records() -> None:
    assert readback.field_values({"fields": None}) == {}
    assert readback.field_values({"fields": {"a": 1}}) == {"a": 1}
    assert readback.record_schema_id({"schemaId": "assaysch_abc123"}) == "assaysch_abc123"
    assert readback.record_schema_id({}) is None
    assert readback.values_equal([1, 2], [1, 2]) is True
    assert f"Sent to entry {ENTRY_ID}." in readback.pending_next_step(2, entry_id=ENTRY_ID)
    assert readback.pending_next_step(0) is None


async def test_verify_links_the_entry_in_next_step(runtime, fake) -> None:
    planned = await applied_plan(runtime, fake, [record(1)])
    fake.complete_upload()
    data = await runtime.planner.verify_results(planned.id)
    url = entry_web_url(ENTRY_ID)
    assert data["web_url"] == url
    assert f"entry {ENTRY_ID} at {url}," in data["next_step"]


def test_pending_next_step_links_the_entry_when_a_web_url_is_known() -> None:
    url = "https://your-tenant-dev.benchling.com/benchling/f/lib_abc123-n/etr_abc123-s/edit"
    step = readback.pending_next_step(1, entry_id=ENTRY_ID, web_url=url)
    assert step is not None and f"entry {ENTRY_ID} at {url}" in step
    assert " at " not in (readback.pending_next_step(1, entry_id=ENTRY_ID) or "")
    assert url not in (readback.pending_next_step(1, web_url=url) or "")
