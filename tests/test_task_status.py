"""`changes status --task`: read one Benchling task by ID with the plan-status poller."""

from __future__ import annotations

import asyncio

import pytest

from benchling_tools.errors import ValidationError
from benchling_tools.plans.planner import ChangePlanner

TASK = "00000000-0000-4000-8000-00000000a001"
CREATED = [
    {"id": f"00000000-0000-4000-8000-{number:012d}", "v3Id": f"result_task{number}"}
    for number in range(1, 4)
]


async def test_succeeded_task_lists_created_result_ids_and_the_readback_step(runtime, fake) -> None:
    fake.tasks[TASK] = {"status": "SUCCEEDED", "response": {"assayResults": CREATED}}
    status = await runtime.planner.task_status(TASK)
    ids = [item["id"] for item in CREATED]
    assert status["task_id"] == TASK
    assert status["task_status"] == "SUCCEEDED"
    assert status["result_state"] == "pending_integration"
    assert status["timed_out"] is False
    assert status["response_kind"] == "assay_results"
    assert status["response_keys"] == ["assayResults"]
    assert status["record_ids"] == ids
    assert status["record_v3_ids"] == [item["v3Id"] for item in CREATED]
    assert status["record_count"] == 3
    assert "results pending --id " + " --id ".join(ids) in status["next_step"]
    assert status["remote_task"] == fake.tasks[TASK]
    assert "message" not in status and "errors" not in status
    assert fake.task_requests == [TASK]
    assert fake.writes() == []


async def test_next_step_names_at_most_twenty_result_ids(runtime, fake) -> None:
    records = [{"id": f"00000000-0000-4000-8000-{n:012d}"} for n in range(25)]
    fake.tasks[TASK] = {"status": "SUCCEEDED", "response": {"assayResults": records}}
    status = await runtime.planner.task_status(TASK)
    assert status["record_count"] == 25
    assert status["next_step"].count("--id ") == 20
    assert records[19]["id"] in status["next_step"]
    assert records[20]["id"] not in status["next_step"]
    assert "first 20 of 25" in status["next_step"]


async def test_failed_task_returns_benchling_message_and_errors(runtime, fake) -> None:
    errors = [{"index": 0, "message": "invalid value"}, {"message": "table-level error"}]
    fake.tasks[TASK] = {"status": "FAILED", "message": "Bulk create failed", "errors": errors}
    status = await runtime.planner.task_status(TASK)
    assert status["result_state"] == "submission_failed"
    assert status["message"] == "Bulk create failed"
    assert status["errors"] == errors
    assert status["record_ids"] == [] and status["record_count"] == 0
    assert status["response_kind"] == "none"
    assert "errors" in status["next_step"]


async def test_other_task_kinds_are_listed_not_interpreted(runtime, fake) -> None:
    fake.tasks[TASK] = {"status": "SUCCEEDED", "response": {"containers": [{"id": "con_1"}]}}
    status = await runtime.planner.task_status(TASK)
    assert status["response_kind"] == "other"
    assert status["response_keys"] == ["containers"]
    assert status["record_ids"] == [] and status["record_count"] == 0
    assert "not interpreted" in status["next_step"]
    assert status["remote_task"]["response"] == {"containers": [{"id": "con_1"}]}


async def test_succeeded_task_with_no_results(runtime, fake) -> None:
    fake.tasks[TASK] = {"status": "SUCCEEDED", "response": {"assayResults": []}}
    status = await runtime.planner.task_status(TASK)
    assert status["record_ids"] == []
    assert "lists no created results" in status["next_step"]


async def test_unknown_task_status_is_reported(runtime, fake) -> None:
    fake.tasks[TASK] = {"status": "QUEUED"}
    status = await runtime.planner.task_status(TASK)
    assert status["result_state"] == "unknown"
    assert "remote_task" in status["next_step"]


async def test_running_task_without_wait_says_to_wait(runtime, fake) -> None:
    fake.tasks[TASK] = {"status": "RUNNING"}
    status = await runtime.planner.task_status(TASK)
    assert status["result_state"] == "submission_running"
    assert status["timed_out"] is False
    assert f"changes status --task {TASK} --wait" in status["next_step"]
    assert fake.task_requests == [TASK]


async def test_wait_polls_running_task_with_the_existing_backoff(
    runtime, fake, monkeypatch
) -> None:
    fake.task_responses[TASK] = [{"status": "RUNNING"}, {"status": "RUNNING"}]
    fake.tasks[TASK] = {"status": "SUCCEEDED", "response": {"assayResults": CREATED[:1]}}
    delays: list[float] = []
    sleep = asyncio.sleep

    async def no_sleep(delay):
        delays.append(delay)
        await sleep(0)

    monkeypatch.setattr(asyncio, "sleep", no_sleep)
    status = await runtime.planner.task_status(TASK, wait=True, timeout=60)
    assert fake.task_requests == [TASK, TASK, TASK]
    assert delays == [0.25, 0.5]
    assert status["result_state"] == "pending_integration"
    assert status["record_ids"] == [CREATED[0]["id"]]


async def test_short_timeout_reports_timed_out(runtime, fake) -> None:
    fake.tasks[TASK] = {"status": "RUNNING"}
    status = await runtime.planner.task_status(TASK, wait=True, timeout=0.1)
    assert status["timed_out"] is True
    assert status["result_state"] == "submission_running"


async def test_task_goes_through_the_plan_status_poller(runtime, fake, monkeypatch) -> None:
    seen = []
    poll = ChangePlanner._poll_result_task

    async def spy(self, task, *, wait, deadline):
        seen.append((task, wait))
        return await poll(self, task, wait=wait, deadline=deadline)

    monkeypatch.setattr(ChangePlanner, "_poll_result_task", spy)
    fake.tasks[TASK] = {"status": "SUCCEEDED", "response": {"assayResults": []}}
    await runtime.planner.task_status(TASK, wait=True, timeout=1)
    assert seen == [({"task_id": TASK, "batch_index": 0, "table_id": None}, True)]


async def test_task_lookup_errors_are_reported_not_raised(runtime, fake) -> None:
    fake.fail("GET", f"/tasks/{TASK}", 404)
    status = await runtime.planner.task_status(TASK)
    assert status["remote_task_error"]["code"] == "not_found"
    assert status["result_state"] == "unknown"
    assert status["remote_task"] is None
    assert "no task with this ID" in status["next_step"]
    assert "will not help" in status["next_step"]


async def test_other_task_lookup_errors_say_to_try_again(runtime, fake) -> None:
    fake.fail("GET", f"/tasks/{TASK}", 400)
    status = await runtime.planner.task_status(TASK)
    assert status["remote_task_error"]["code"] != "not_found"
    assert "remote_task_error" in status["next_step"]
    assert "again" in status["next_step"]


@pytest.mark.parametrize(
    "task_id", ["", "task/abc", "../tasks", "task abc", "task\tabc", ".", None]
)
async def test_invalid_task_ids_are_rejected_before_any_request(runtime, fake, task_id) -> None:
    with pytest.raises(ValidationError, match="task_id"):
        await runtime.planner.task_status(task_id)
    with pytest.raises(ValidationError, match="task_id"):
        await runtime.service.get_task(task_id)
    assert fake.task_requests == []


@pytest.mark.parametrize(
    ("arguments", "message"),
    [
        ({"wait": "yes"}, "wait must be a boolean"),
        ({"timeout": 0}, "timeout must be greater than 0"),
        ({"timeout": 3601}, "timeout must be greater than 0"),
    ],
)
async def test_task_status_rejects_invalid_wait_arguments(runtime, arguments, message) -> None:
    with pytest.raises(ValidationError, match=message):
        await runtime.planner.task_status(TASK, **arguments)
