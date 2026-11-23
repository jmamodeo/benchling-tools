"""Plan/apply orchestration: builds checksummed change plans and applies them with a journal."""

from __future__ import annotations

import asyncio
import uuid
from copy import deepcopy
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, NoReturn

from filelock import FileLock
from filelock import Timeout as FileLockTimeout

from .. import capacity
from ..change_spec import parse_change_specification
from ..errors import (
    BenchlingAgentError,
    ConflictError,
    PartialCompletionError,
    UpstreamError,
    ValidationError,
)
from ..models import (
    ChangeJournal,
    ChangePlan,
    PlanFamily,
    PlanStatus,
)
from ..services import assay_result_lookup, require_task_id
from . import readback
from .containers import ContainerPlanning
from .document import DocumentPlanning
from .entry import EntryPlanning
from .fields import FieldsPlanning
from .repository import checksum_for, json_diff
from .result_update import request_is_result_update
from .results import (
    ResultsPlanning,
    pending_results_next_step,
    plan_entry_web_url,
    result_upload_batches,
)
from .table import TablePlanning

# Result IDs named in the `results pending --id` next step of `changes status --task`.
_NEXT_STEP_IDS = 20


def _check_wait(wait: object, timeout: object) -> None:
    """Validate the --wait and --timeout values shared by plan and task status."""
    if not isinstance(wait, bool):
        raise ValidationError("wait must be a boolean")
    if (
        isinstance(timeout, bool)
        or not isinstance(timeout, (int, float))
        or timeout < 0.1
        or timeout > 3600
    ):
        raise ValidationError("timeout must be greater than 0 and at most 3600 seconds")


def _task_next_step(status: dict[str, Any]) -> str:
    """What to do next after reading one task with `changes status --task`."""
    state = status["result_state"]
    task_id = status["task_id"]
    error = status.get("remote_task_error")
    if isinstance(error, dict) and error.get("code") == "not_found":
        return (
            "Benchling has no task with this ID. It keeps a task only for a while (its 404 "
            "says the task may have expired), so running this again will not help. Read the "
            "created results with `results pending --id …` from the app's log, or with "
            "`results list --entry ENTRY` once they are accepted."
        )
    if "remote_task_error" in status:
        return (
            "The task could not be read; see remote_task_error, then run "
            f"`changes status --task {task_id}` again."
        )
    if state == "submission_running":
        return f"The task is still running; run `changes status --task {task_id} --wait`."
    if state == "submission_failed":
        return "The task failed; check message and errors for what Benchling reported."
    if state == "pending_integration":
        ids = status["record_ids"]
        if ids:
            shown = ids[:_NEXT_STEP_IDS]
            command = "results pending " + " ".join(f"--id {item}" for item in shown)
            more = (
                f" This names the first {len(shown)} of {len(ids)}; record_ids lists them all."
                if len(ids) > len(shown)
                else ""
            )
            return f"The task succeeded. Read the created results back with `{command}`.{more}"
        if status["response_kind"] == "other":
            return (
                "The task succeeded. Its response is not a list of assay results, so it is "
                "not interpreted; response_keys lists its keys and remote_task has it in full."
            )
        return "The task succeeded, but its response lists no created results."
    return "The task status is not one Benchling documents; see remote_task."


# Journal states after an apply that may have created records.
_APPLIED_STATUSES = frozenset({PlanStatus.SUCCEEDED, PlanStatus.PARTIAL, PlanStatus.UNKNOWN})
# Pages of 100 read per result schema by ``results capacity --include-schema-pending``.
_SCHEMA_PENDING_PAGES = 20
# Pages of 100 read per payload schema by ``results verify --payload --created-after``.
_PAYLOAD_PAGES = 100
# One verified batch: the batch, its fetched records, its diff, and extra report keys.
_VerifyUnit = tuple[
    dict[str, Any], list[dict[str, Any]], dict[str, list[dict[str, Any]]], dict[str, Any]
]


def _is_result_creation_event(stage: str) -> bool:
    """Recognize the current and legacy result-upload journal stage names."""
    return stage in {"create_results", "submit_results"}


def _with_http_status(details: dict[str, Any]) -> dict[str, Any]:
    """Copy error details and record the raw HTTP status when the API returned one."""
    recorded = dict(details)
    status = recorded.get("status")
    if isinstance(status, int) and "http_status" not in recorded:
        recorded["http_status"] = status
    return recorded


def _failure_details(exc: BenchlingAgentError) -> dict[str, Any]:
    """Journal details for a failed apply step, including the HTTP status when known."""
    details: dict[str, Any] = {"error": exc.message}
    status = exc.details.get("status")
    if isinstance(status, int):
        details["http_status"] = status
    return details


class ChangePlanner(
    EntryPlanning,
    DocumentPlanning,
    TablePlanning,
    FieldsPlanning,
    ResultsPlanning,
    ContainerPlanning,
):
    """Creates change plans and applies them under a per-plan file lock."""

    async def plan(
        self, reference: str | None, family: PlanFamily | str, request: dict[str, Any]
    ) -> ChangePlan:
        """Build and persist a checksummed plan without writing to the entry.

        Raises ValidationError for an unknown family or invalid request.
        """
        try:
            family = PlanFamily(family)
        except ValueError as exc:
            raise ValidationError(
                "Unknown change family",
                details={"family": str(family), "supported": [item.value for item in PlanFamily]},
            ) from exc
        request = deepcopy(request)
        if family == PlanFamily.CONTAINERS:
            request = parse_change_specification(request)
        if family != PlanFamily.RESULTS and request_is_result_update(request):
            raise ValidationError(
                "CSV result updates require the results family",
                details={"family": family.value},
            )
        if family == PlanFamily.ENTRY:
            if reference is not None:
                raise ValidationError("Entry creation plans must not specify --entry")
            entry_id = None
            before, after, preconditions, guarantees, warnings = await self._plan_entry_create(
                request
            )
        elif family == PlanFamily.CONTAINERS:
            parts = await self._plan_containers(request)
            before, after, preconditions, guarantees, warnings = parts
            entry_id = None
        else:
            if not isinstance(reference, str) or not reference.strip():
                raise ValidationError("Entry reference is required for this change family")
            resolved = await self.service.resolve_entry(reference)
            entry_id = resolved["entry_id"]
            planners = {
                PlanFamily.DOCUMENT: self._plan_document,
                PlanFamily.TABLE: self._plan_table,
                PlanFamily.FIELDS: self._plan_fields,
                PlanFamily.RESULTS: self._plan_results,
            }
            before, after, preconditions, guarantees, warnings = await planners[family](
                entry_id, request
            )
        plan_data: dict[str, Any] = {
            "schema_version": "1.0",
            "id": uuid.uuid4().hex,
            "created_at": datetime.now(UTC).isoformat(),
            "env": self.api.environment.name,
            "tenant_url": self.api.environment.base_url,
            "entry_id": entry_id,
            "family": family.value,
            "request": request,
            "before": before,
            "after": after,
            "diff": json_diff(before, after),
            "preconditions": preconditions,
            "guarantees": guarantees,
            "warnings": warnings,
        }
        # Hash the normalized Pydantic representation so datetimes and enums
        # verify identically after the plan is read from disk.
        plan_data["checksum"] = ""
        plan = ChangePlan.model_validate(plan_data)
        plan.checksum = checksum_for(plan.model_dump(mode="json"))
        self.repository.save_plan(plan)
        return plan

    async def apply(self, plan_id: str) -> dict[str, Any]:
        """Apply a stored plan while holding its lock.

        Raises ConflictError if another process holds the lock for 30 seconds.
        """
        self.repository._validate_id(plan_id)
        self.repository.config.ensure()
        lock = FileLock(
            str(self.repository.config.locks_dir / f"plan-{plan_id}.apply.lock"),
            thread_local=False,
        )
        try:
            await asyncio.to_thread(lock.acquire, timeout=30)
        except FileLockTimeout as exc:
            raise ConflictError(
                "Another process is applying this plan",
                details={"plan_id": plan_id},
            ) from exc
        try:
            return await self._apply_locked(plan_id)
        finally:
            lock.release()

    def _check_plan_scope(self, plan: ChangePlan) -> None:
        """Reject plans created for a different environment or tenant."""
        if (
            plan.env != self.api.environment.name
            or plan.tenant_url != self.api.environment.base_url
        ):
            raise ValidationError(
                "Plan belongs to a different environment or tenant",
                details={"plan_env": plan.env, "active_env": self.api.environment.name},
            )

    async def _run_family_apply(self, plan: ChangePlan, journal: ChangeJournal) -> dict[str, Any]:
        """Dispatch to the apply routine for the plan's family."""
        if plan.family == PlanFamily.ENTRY:
            return await self._apply_entry_create(plan, journal)
        appliers = {
            PlanFamily.DOCUMENT: self._apply_document,
            PlanFamily.TABLE: self._apply_table,
            PlanFamily.FIELDS: self._apply_fields,
            PlanFamily.RESULTS: self._apply_results,
            PlanFamily.CONTAINERS: self._apply_containers,
        }
        return await appliers[plan.family](plan, journal)

    async def _apply_locked(self, plan_id: str) -> dict[str, Any]:
        plan = self.repository.load_plan(plan_id)
        self._check_plan_scope(plan)
        journal = self.repository.load_journal(plan_id)
        if journal.status != PlanStatus.PENDING:
            raise ConflictError(
                "Plan has already been applied or attempted",
                details={"plan_id": plan_id, "status": journal.status.value},
            )
        self._event(journal, "apply", "started", {"family": plan.family.value})
        journal.status = PlanStatus.APPLYING
        self.repository.save_journal(journal)
        try:
            result = await self._run_family_apply(plan, journal)
            return self._finalize_success(plan, journal, result)
        except PartialCompletionError as exc:
            self._record_unknown(plan_id, journal, exc)
            raise
        except BenchlingAgentError as exc:
            self._record_error(plan_id, journal, exc)
            raise
        except Exception as exc:
            self._raise_local_failure(plan_id, journal, exc)

    def _finalize_success(
        self, plan: ChangePlan, journal: ChangeJournal, result: dict[str, Any]
    ) -> dict[str, Any]:
        """Mark the journal succeeded and build the apply response."""
        journal.status = PlanStatus.SUCCEEDED
        journal.result = result
        self._event(journal, "apply", "succeeded", result)
        self.repository.save_journal(journal)
        response: dict[str, Any] = {
            "plan": plan.model_dump(mode="json"),
            "journal": journal.model_dump(mode="json"),
        }
        web_url: str | None = None
        if "web_url" in result:
            web_url = result["web_url"]
            response["web_url"] = web_url
        next_step = pending_results_next_step(plan, web_url=web_url)
        if next_step:
            response["next_step"] = next_step
        return response

    def _record_unknown(
        self, plan_id: str, journal: ChangeJournal, exc: PartialCompletionError
    ) -> None:
        """Record an unknown outcome and attach plan and journal to the error details."""
        journal.status = PlanStatus.UNKNOWN
        self._event(journal, "apply", "unknown", _with_http_status(exc.details))
        self.repository.save_journal(journal)
        plan = self.repository.load_plan(plan_id)
        submitted_tables: set[str] = set()
        for event in journal.events:
            table_id = event.details.get("table_id")
            if (
                _is_result_creation_event(event.stage)
                and event.status in {"succeeded", "unknown"}
                and isinstance(table_id, str)
            ):
                submitted_tables.add(table_id)
        next_step = pending_results_next_step(plan, submitted_tables or None)
        if exc.details.get("next_step"):
            next_step = " ".join(
                item
                for item in (exc.details["next_step"], next_step if submitted_tables else None)
                if item
            )
        exc.details = {
            **exc.details,
            "plan_id": plan_id,
            "journal": journal.model_dump(mode="json"),
            **({"next_step": next_step} if next_step else {}),
        }

    def _record_error(self, plan_id: str, journal: ChangeJournal, exc: BenchlingAgentError) -> None:
        """Record a failed or partial apply; raises PartialCompletionError if a step had succeeded."""
        if any(event.status == "succeeded" for event in journal.events if event.stage != "apply"):
            journal.status = PlanStatus.PARTIAL
            self._event(journal, "apply", "partial", _failure_details(exc))
            self.repository.save_journal(journal)
            plan = self.repository.load_plan(plan_id)
            submitted_tables: set[str] = set()
            for event in journal.events:
                table_id = event.details.get("table_id")
                if (
                    _is_result_creation_event(event.stage)
                    and event.status == "succeeded"
                    and isinstance(table_id, str)
                ):
                    submitted_tables.add(table_id)
            next_step = pending_results_next_step(plan, submitted_tables or None)
            if exc.details.get("next_step"):
                next_step = " ".join(
                    item
                    for item in (exc.details["next_step"], next_step if submitted_tables else None)
                    if item
                )
            raise PartialCompletionError(
                "Some plan steps completed before a later step failed",
                details={
                    "plan_id": plan_id,
                    "cause": exc.message,
                    "journal": journal.model_dump(mode="json"),
                    **(
                        {"orphaned_blob_ids": exc.details["orphaned_blob_ids"]}
                        if "orphaned_blob_ids" in exc.details
                        else {}
                    ),
                    **({"next_step": next_step} if next_step else {}),
                },
            ) from exc
        journal.status = PlanStatus.FAILED
        self._event(journal, "apply", "failed", _failure_details(exc))
        self.repository.save_journal(journal)

    def _raise_local_failure(
        self, plan_id: str, journal: ChangeJournal, exc: Exception
    ) -> NoReturn:
        """Record an unexpected local failure and raise the wrapped error."""
        has_completed_step = any(
            event.status == "succeeded" for event in journal.events if event.stage != "apply"
        )
        journal.status = PlanStatus.PARTIAL if has_completed_step else PlanStatus.FAILED
        self._event(
            journal,
            "apply",
            journal.status.value,
            {"error": type(exc).__name__},
        )
        self.repository.save_journal(journal)
        wrapped = UpstreamError(
            "An unexpected local failure interrupted plan execution",
            details={
                "plan_id": plan_id,
                "type": type(exc).__name__,
                "journal": journal.model_dump(mode="json"),
            },
        )
        if has_completed_step:
            raise PartialCompletionError(
                "A local failure occurred after at least one write completed",
                details=wrapped.details,
            ) from exc
        raise wrapped from exc

    async def status(
        self, plan_reference: str, *, wait: bool = False, timeout: float = 300.0
    ) -> dict[str, Any]:
        """Return the plan and journal; for submitted results, also reconcile the remote task.

        Remote lookup failures are reported under remote_task_error, not raised.
        """
        _check_wait(wait, timeout)
        plan, journal = self._load_plan_and_journal(plan_reference)
        self._check_plan_scope(plan)
        status: dict[str, Any] = {
            "plan": plan.model_dump(mode="json"),
            "journal": journal.model_dump(mode="json"),
        }
        tasks = self._result_tasks(journal)
        if plan.family == PlanFamily.RESULTS and tasks:
            deadline = asyncio.get_running_loop().time() + timeout
            results = []
            updates = []
            for task in tasks:
                task_result = await self._poll_result_task(task, wait=wait, deadline=deadline)
                results.append(task_result)
                if "remote_task" in task_result:
                    updates.append(task_result)
            status["tasks"] = results
            if len(results) == 1:
                status["remote_task"] = results[0].get("remote_task")
                status["result_state"] = results[0].get("result_state", "unknown")
                status["record_ids"] = results[0].get("record_ids", [])
                status["record_v3_ids"] = results[0].get("record_v3_ids", [])
                if "remote_task_error" in results[0]:
                    status["remote_task_error"] = results[0]["remote_task_error"]
            status.update(self._row_errors_by_table(results))
            if updates:
                lock = FileLock(
                    str(self.repository.config.locks_dir / f"plan-{plan.id}.apply.lock"),
                    thread_local=False,
                )
                await asyncio.to_thread(lock.acquire, timeout=30)
                try:
                    journal_path = self.repository.config.journals_dir / f"{plan.id}.json"
                    latest = (
                        self.repository.load_journal(plan.id) if journal_path.is_file() else journal
                    )
                    if latest.result is None:
                        latest.result = {}
                    known = latest.result.get("batches")
                    if not isinstance(known, list):
                        known = []
                    by_task = {
                        item.get("task_id"): item for item in known if isinstance(item, dict)
                    }
                    for update in updates:
                        task_id = update["task_id"]
                        old = by_task.get(task_id, {})
                        by_task[task_id] = {
                            **old,
                            "batch_index": update["batch_index"],
                            "table_id": update["table_id"],
                            "task_id": task_id,
                            "plan_row_offset": update.get("plan_row_offset", 0),
                            "record_ids": update.get("record_ids", []),
                            "record_v3_ids": update.get("record_v3_ids", []),
                            "task_status": update.get("task_status"),
                            "state": update.get("result_state", "unknown"),
                            "row_errors": update.get("row_errors", []),
                            "unmatched_errors": update.get("unmatched_errors", []),
                        }
                    latest.result["batches"] = list(by_task.values())
                    if len(tasks) == 1 and latest.status == PlanStatus.SUCCEEDED:
                        only = by_task[tasks[0]["task_id"]]
                        latest.result.update(
                            {
                                "record_ids": only.get("record_ids", []),
                                "record_v3_ids": only.get("record_v3_ids", []),
                                "task_status": only.get("task_status"),
                                "state": only.get("state"),
                            }
                        )
                    self.repository.save_journal(latest)
                    status["journal"] = latest.model_dump(mode="json")
                finally:
                    lock.release()
        return status

    async def task_status(
        self, task_id: str, *, wait: bool = False, timeout: float = 300.0
    ) -> dict[str, Any]:
        """Read one Benchling task by ID with the plan-status poller. Nothing is written.

        Remote lookup failures are reported under remote_task_error, not raised.
        """
        _check_wait(wait, timeout)
        task_id = require_task_id(task_id)
        deadline = asyncio.get_running_loop().time() + timeout
        polled = await self._poll_result_task(
            {"task_id": task_id, "batch_index": 0, "table_id": None},
            wait=wait,
            deadline=deadline,
        )
        remote = polled.get("remote_task")
        response = remote.get("response") if isinstance(remote, dict) else None
        if not isinstance(response, dict):
            response_kind = "none"
        elif "assayResults" in response:
            response_kind = "assay_results"
        else:
            response_kind = "other"
        record_ids = polled.get("record_ids", [])
        result: dict[str, Any] = {
            "task_id": task_id,
            "task_status": polled.get("task_status"),
            "result_state": polled.get("result_state", "unknown"),
            "timed_out": polled.get("timed_out", False),
            "response_kind": response_kind,
            "response_keys": sorted(response) if isinstance(response, dict) else [],
            "record_ids": record_ids,
            "record_v3_ids": polled.get("record_v3_ids", []),
            "record_count": len(record_ids),
        }
        if isinstance(remote, dict) and polled.get("task_status") == "FAILED":
            result["message"] = remote.get("message")
            result["errors"] = remote.get("errors")
        if "remote_task_error" in polled:
            result["remote_task_error"] = polled["remote_task_error"]
        result["next_step"] = _task_next_step(result)
        result["remote_task"] = remote
        return result

    def _load_plan_and_journal(self, plan_reference: str) -> tuple[ChangePlan, ChangeJournal]:
        """Load a plan ID or portable plan file and its journal."""
        plan_file = Path(plan_reference)
        if (
            plan_file.is_file()
            or plan_file.suffix.casefold() == ".json"
            or plan_file.parent != Path(".")
        ):
            plan, bundled_journal = self.repository.load_plan_document(plan_file)
            journal = self.repository.find_plan_file_journal(plan.id, plan_file, bundled_journal)
            if journal is None:
                raise ValidationError(
                    "The plan file's applied journal is missing. Export it with "
                    "`changes export PLAN_ID --output FILE` from the source home.",
                    details={"plan_id": plan.id},
                )
        else:
            plan = self.repository.load_plan(plan_reference)
            journal = self.repository.load_journal(plan.id)
        return plan, journal

    def _readback_plan(self, plan_reference: str) -> tuple[ChangePlan, ChangeJournal]:
        """Load an applied result upload plan for read-back in the active environment."""
        plan, journal = self._load_plan_and_journal(plan_reference)
        self._check_plan_scope(plan)
        if plan.family != PlanFamily.RESULTS or plan.preconditions.get("operation") == "update":
            raise ValidationError(
                "Only result upload plans can be read back",
                details={"plan_id": plan.id, "family": plan.family.value},
            )
        if journal.status == PlanStatus.PENDING:
            raise ValidationError(
                "Plan has not been applied yet; run `changes apply` first",
                details={"plan_id": plan.id, "status": journal.status.value},
            )
        return plan, journal

    async def _submitted_batches(self, journal: ChangeJournal) -> list[dict[str, Any]]:
        """Each submitted batch with its server-assigned record IDs.

        IDs come from the journal. When ``changes status`` has not stored them yet, the
        task is read once without waiting. The journal is not written.
        """
        result = journal.result or {}
        stored_batches = result.get("batches")
        known = {
            item.get("task_id"): item
            for item in (stored_batches if isinstance(stored_batches, list) else [])
            if isinstance(item, dict)
        }
        submitted: list[dict[str, Any]] = []
        for task in self._result_tasks(journal):
            task_id = task["task_id"]
            stored = dict(known.get(task_id) or {})
            if result.get("task_id") == task_id:
                stored = {**result, **stored}
            entry: dict[str, Any] = {
                "batch_index": task.get("batch_index", 0),
                "table_id": task.get("table_id"),
                "task_id": task_id,
                "record_ids": [
                    item
                    for item in stored.get("record_ids") or stored.get("record_v3_ids") or []
                    if isinstance(item, str)
                ],
                "task_status": stored.get("task_status"),
            }
            if not entry["record_ids"]:
                polled = await self._poll_result_task(task, wait=False, deadline=0.0)
                entry["record_ids"] = polled.get("record_ids") or polled.get("record_v3_ids", [])
                entry["task_status"] = polled.get("task_status", entry["task_status"])
                if "remote_task_error" in polled:
                    entry["remote_task_error"] = polled["remote_task_error"]
            submitted.append(entry)
        return submitted

    @staticmethod
    def _unsubmitted_reason(journal: ChangeJournal, batch_index: int) -> str:
        """Why a planned batch has no task: the journal's submit outcome for it."""
        for event in journal.events:
            if (
                _is_result_creation_event(event.stage)
                and event.details.get("batch_index") == batch_index
            ):
                if event.status == "failed":
                    return "submission_failed"
                if event.status == "unknown":
                    return "submission_unconfirmed"
        return "not_submitted"

    @staticmethod
    def _no_ids_reason(batch: dict[str, Any]) -> str:
        if "remote_task_error" in batch:
            return "task_unreadable"
        return {
            "RUNNING": "task_running",
            "FAILED": "task_failed",
        }.get(str(batch.get("task_status")), "no_record_ids")

    @staticmethod
    def _readback_limit(value: object, label: str, maximum: int) -> int:
        if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= maximum:
            raise ValidationError(f"{label} must be between 1 and {maximum}")
        return value

    def _entry_upload_plans(
        self, entry_id: str
    ) -> list[tuple[ChangePlan, ChangeJournal, datetime]]:
        """Applied result upload plans in this home for one entry, newest first.

        Each plan comes with the time its apply finished. Result updates, plans for
        another environment or entry, plans never applied or that failed with no write,
        and files that cannot be read are skipped.
        """
        found: list[tuple[ChangePlan, ChangeJournal, datetime]] = []
        for plan_id in self.repository.list_plan_ids():
            try:
                plan = self.repository.load_plan(plan_id)
                journal = self.repository.load_journal(plan_id)
            except (BenchlingAgentError, OSError, ValueError):
                continue
            if (
                plan.family != PlanFamily.RESULTS
                or plan.preconditions.get("operation") == "update"
                or plan.entry_id != entry_id
                or plan.env != self.api.environment.name
                or plan.tenant_url != self.api.environment.base_url
                or journal.status not in _APPLIED_STATUSES
            ):
                continue
            finished = [
                event.at
                for event in journal.events
                if event.stage == "apply" and event.status != "started"
            ]
            found.append((plan, journal, finished[-1] if finished else plan.created_at))
        found.sort(key=lambda item: (item[2], item[0].id), reverse=True)
        return found

    async def _read_records(
        self, table_of: dict[str, Any]
    ) -> tuple[list[dict[str, Any]], list[str]]:
        """Read records by id and summarize each with the table it was uploaded to."""
        if not table_of:
            return [], []
        fetched = await self.service.get_assay_results(list(table_of))
        items = [
            readback.summarize_record(
                record,
                table_id=table_of.get(record.get("id"), table_of.get(record.get("v3Id"))),
            )
            for record in fetched["items"]
        ]
        return items, fetched["not_found"]

    @staticmethod
    def _pending_tables(items: list[dict[str, Any]], names: dict[Any, Any]) -> list[str]:
        """Names of the tables holding pending items, in first-seen order."""
        return list(
            dict.fromkeys(
                str(names.get(item.get("table_id")) or item.get("table_id"))
                for item in items
                if item["state"] == readback.PENDING
            )
        )

    async def _entry_readback(self, entry_id: str) -> dict[str, Any]:
        """Records of every applied result upload plan for the entry in this home.

        Returns ``items`` (each with its ``table_id`` and state), ``not_found``,
        ``plans``, table ``names``, and the first recorded entry ``web_url``.
        """
        web_url: str | None = None
        names: dict[Any, Any] = {}
        table_of: dict[str, Any] = {}
        plans: list[dict[str, Any]] = []
        for plan, journal, applied_at in self._entry_upload_plans(entry_id):
            submitted = await self._submitted_batches(journal)
            names.update(
                {batch["table_id"]: batch["table_name"] for batch in readback.planned_batches(plan)}
            )
            web_url = web_url or plan_entry_web_url(plan)
            for batch in submitted:
                for record_id in batch["record_ids"]:
                    table_of.setdefault(record_id, batch.get("table_id"))
            plans.append(
                {
                    "plan_id": plan.id,
                    "applied_at": applied_at.isoformat(),
                    "record_count": sum(len(batch["record_ids"]) for batch in submitted),
                }
            )
        items, not_found = await self._read_records(table_of)
        return {
            "items": items,
            "not_found": not_found,
            "plans": plans,
            "names": names,
            "web_url": web_url,
        }

    async def _entry_pending_records(self, entry_id: str) -> list[dict[str, Any]]:
        known = await self._entry_readback(entry_id)
        return [item for item in known["items"] if item["state"] == readback.PENDING]

    async def schema_pending_records(
        self, schema_id: str, created_after: str
    ) -> tuple[list[dict[str, Any]], bool]:
        """Pending V2 results of one result schema created at or after a time, on any entry.

        V2 ``GET /assay-results`` filters by ``schemaId`` and ``createdAt.gte`` but has
        no entry filter, and a pending result has no entry until it is accepted, so only
        records whose ``entryId`` is null (and that are not archived) are kept. At most
        ``_SCHEMA_PENDING_PAGES`` pages of 100 are read; the flag is false when more
        remain. ``results capacity --include-schema-pending`` and ``--include-pending``
        with ``--pending-created-after`` both use it.
        """
        records: list[dict[str, Any]] = []
        cursor: str | None = None
        pages = 0
        while pages < _SCHEMA_PENDING_PAGES:
            page = await self.service.list_assay_results(
                schema_id=schema_id, created_after=created_after, page_size=100, cursor=cursor
            )
            pages += 1
            records.extend(
                item
                for item in page["items"]
                if isinstance(item, dict)
                and item.get("entryId") is None
                and readback.result_state(item) == readback.PENDING
            )
            cursor = page["next_token"]
            if not cursor:
                break
        return records, cursor is None

    async def entry_pending_results(
        self,
        entry_id: str,
        *,
        created_after: str | None = None,
        schema_ids: list[str] | None = None,
    ) -> dict[str, Any]:
        """Still-pending results for one entry, from this home's plans and a schema window.

        Benchling has no API filter for pending results by entry, so the records come
        from the same plans and journals ``results pending --entry`` reads (``source``
        ``this_home``, each with its ``table_id``). With ``created_after``, pending
        results of each schema in ``schema_ids`` created at or after it are added
        (``source`` ``schema_window``, ``table_id`` null): Benchling ties them to no
        entry or table, so they may come from another entry. Records are deduplicated
        by V2 id and this_home wins. Returns ``items``, ``plans``, ``not_found``,
        table ``names``, the entry ``web_url``, and ``sources``.
        """
        known = await self._entry_readback(entry_id)
        items = [
            {**item, "source": "this_home"}
            for item in known["items"]
            if item["state"] == readback.PENDING
        ]
        known_ids = {
            str(key)
            for item in known["items"]
            for key in (item.get("v2_id"), item.get("v3_id"))
            if key
        }
        this_home = len(items)
        window: dict[str, Any] | None = None
        if created_after is not None:
            schemas = list(dict.fromkeys(item for item in schema_ids or [] if item))
            complete = True
            added = 0
            for schema_id in schemas:
                records, done = await self.schema_pending_records(schema_id, created_after)
                complete = complete and done
                for record in records:
                    v2_id = record.get("id")
                    if not isinstance(v2_id, str) or v2_id in known_ids:
                        continue
                    known_ids.add(v2_id)
                    items.append(
                        readback.summarize_record(record, table_id=None, source="schema_window")
                    )
                    added += 1
            window = {
                "created_after": created_after,
                "schema_ids": schemas,
                "count": added,
                "complete": complete,
                "note": readback.SCHEMA_WINDOW_NOTE,
            }
        known["items"] = items
        known["sources"] = {
            "this_home": {"plans": len(known["plans"]), "count": this_home},
            "schema_window": window,
        }
        return known

    async def results_list_pending(
        self,
        entry: str,
        *,
        schema_id: str | None = None,
        result_ids: list[str] | None = None,
        limit: int = 100,
        created_after: str | None = None,
    ) -> dict[str, Any]:
        """The ``pending`` block ``results list --include-pending`` adds to a page.

        Pending results have no entry until accepted, so V3 ``entry.eq`` does not
        list them. They are read back from this home's applied upload plans for the
        entry and, with ``created_after``, from the result schema of each results table
        on the entry (``entry_pending_results``), and kept when they match
        ``schema_id`` and ``result_ids`` (V2 or V3 ids). At most ``limit`` are listed;
        ``count`` covers every pending record found.
        """
        limit = self._readback_limit(limit, "limit", 100)
        resolved = await self.service.resolve_entry(entry)
        entry_id = str(resolved["entry_id"])
        schema_ids: list[str] | None = None
        if created_after is not None:
            tables = (await self.service.list_result_tables(entry_id))["items"]
            schema_ids = [
                str(table["schema_id"])
                for table in tables
                if isinstance(table.get("schema_id"), str)
                and (schema_id is None or table["schema_id"] == schema_id)
            ]
        found = await self.entry_pending_results(
            entry_id, created_after=created_after, schema_ids=schema_ids
        )
        wanted = set(result_ids) if result_ids else None
        items = [
            item
            for item in found["items"]
            if (schema_id is None or item.get("schema_id") == schema_id)
            and (wanted is None or item.get("v2_id") in wanted or item.get("v3_id") in wanted)
        ]
        placed = [item for item in items if item.get("table_id") is not None]
        if items:
            next_step = readback.pending_next_step(
                len(items),
                entry_id=entry_id,
                table_names=self._pending_tables(placed, found["names"]) or None,
                web_url=resolved.get("web_url") or found["web_url"],
            )
        elif not found["plans"] and created_after is None:
            next_step = readback.NO_ENTRY_PLANS_NEXT_STEP
        else:
            next_step = None
        return {
            "sources": found["sources"],
            "entry_id": entry_id,
            "plans": found["plans"],
            "count": len(items),
            "items": items[:limit],
            "truncated": len(items) > limit,
            "not_found": found["not_found"][:limit],
            "next_step": next_step,
        }

    async def results_capacity(
        self,
        entry: str,
        *,
        plan_id: str | None = None,
        spec: dict[str, Any] | None = None,
        include_schema_pending: bool = False,
        created_after: str | None = None,
    ) -> dict[str, Any]:
        """Report each results table's rows, known pending results, planned rows, and room.

        With ``plan_id`` (a saved plan or portable bundle) or ``spec`` (a result upload
        spec) the planned rows are counted per table; otherwise current usage only. It
        is the check ``changes plan --family results`` runs. Nothing is written or saved.
        ``include_schema_pending`` with ``created_after`` also counts pending results of
        each table's schema created since then on any entry; that count is reported
        and not used for ``fits``.
        """
        if plan_id is not None and spec is not None:
            raise ValidationError("Pass only one of --plan, --spec, or --spec-json")
        if include_schema_pending != (created_after is not None):
            raise ValidationError("--include-schema-pending and --created-after go together")
        resolved = await self.service.resolve_entry(entry)
        entry_id = resolved["entry_id"]
        planned: dict[str, int] = {}
        warnings: list[str] = []
        source: dict[str, Any] = {"mode": "current"}
        if plan_id is not None:
            plan, journal = self._load_plan_and_journal(plan_id)
            self._check_plan_scope(plan)
            if plan.family != PlanFamily.RESULTS or plan.preconditions.get("operation") == "update":
                raise ValidationError(
                    "Only result upload plans add rows to a results table",
                    details={"plan_id": plan.id, "family": plan.family.value},
                )
            if plan.entry_id != entry_id:
                raise ValidationError(
                    "The plan is for another entry",
                    details={
                        "plan_id": plan.id,
                        "entry_id": entry_id,
                        "plan_entry_id": plan.entry_id,
                    },
                )
            source = {"mode": "plan", "plan_id": plan.id, "status": journal.status.value}
            if journal.status == PlanStatus.PENDING:
                for batch in readback.planned_batches(plan):
                    planned[str(batch["table_id"])] = len(batch["records"])
            else:
                warnings.append(
                    "The plan was already applied, so its rows are not counted as planned. "
                    "Rows it created are pending_known until accepted, then row_count"
                )
        elif spec is not None:
            if request_is_result_update(spec):
                raise ValidationError("A result update does not add rows to a results table")
            for batch in result_upload_batches(spec):
                planned[batch["table_id"]] = len(batch["assay_results"])
            source = {"mode": "spec"}
        report = await self.results_table_capacity(entry_id, planned, all_tables=True)
        if include_schema_pending:
            assert created_after is not None
            await self._schema_pending(report, created_after)
        return {
            "resolved": resolved,
            "source": source,
            "limit": report["limit"],
            "tables": report["tables"],
            "fits": report["fits"],
            "warnings": [*warnings, *report["warnings"]],
            "next_step": capacity.next_step(report),
        }

    async def _schema_pending(self, report: dict[str, Any], created_after: str) -> None:
        """Add ``pending_schema`` to each table: pending results of its schema since a time.

        V2 ``GET /assay-results`` has no entry filter, so the count covers every entry
        with that schema. At most ``_SCHEMA_PENDING_PAGES`` pages of 100 are read per
        schema; ``pending_schema_complete`` is false when more remain.
        """
        counted: dict[str, tuple[int, bool]] = {}
        for table in report["tables"]:
            schema_id = table.get("schema_id")
            if not isinstance(schema_id, str):
                table["pending_schema"] = None
                continue
            if schema_id not in counted:
                records, complete = await self.schema_pending_records(schema_id, created_after)
                counted[schema_id] = (len(records), complete)
            pending, complete = counted[schema_id]
            table["pending_schema"] = pending
            table["pending_schema_complete"] = complete
            used = table.get("used_rows")
            table["remaining_with_schema_pending"] = (
                capacity.RESULTS_TABLE_ROW_LIMIT - used - pending - table["planned"]
                if isinstance(used, int)
                else None
            )
        report["warnings"].append(
            f"pending_schema counts pending results of each table's schema created at or after "
            f"{created_after} on any entry, because V2 assay-result lists have no entry filter. "
            "It is not used for fits"
        )

    async def read_back_results(
        self,
        *,
        entry: str | None = None,
        plan_id: str | None = None,
        result_ids: list[str] | None = None,
        schema_id: str | None = None,
        created_after: str | None = None,
        limit: int = 50,
        cursor: str | None = None,
    ) -> dict[str, Any]:
        """Fetch result records, including pending ones, and mark each pending or accepted.

        Exactly one source: an entry (the record IDs of every applied result upload plan
        for it in this home), a result upload plan ID (its journal record IDs), explicit
        result IDs (V2 UUIDs or V3 ``result_…`` ids, mixed freely), or a result schema ID
        with a created-after time. Each item has ``v2_id`` and ``v3_id``; each ID not found
        is listed in ``not_found_details`` with its namespace and the lookup used. At most ``limit``
        records are listed; counts cover every record read. Nothing is written.
        """
        limit = self._readback_limit(limit, "limit", 100)
        by_schema = schema_id is not None or created_after is not None
        if sum([entry is not None, plan_id is not None, result_ids is not None, by_schema]) != 1:
            raise ValidationError(
                "Pass exactly one of entry, plan_id, result_ids, or schema_id with created_after"
            )
        if cursor is not None and not by_schema:
            raise ValidationError("cursor applies only to schema_id with created_after")
        if entry is not None and (not isinstance(entry, str) or not entry.strip()):
            raise ValidationError("entry must be an entry ID, display ID, or URL")
        response: dict[str, Any]
        items: list[dict[str, Any]]
        not_found: list[str] = []
        next_step: str | None
        if by_schema:
            if schema_id is None or created_after is None:
                raise ValidationError("schema_id and created_after must be passed together")
            page = await self.service.list_assay_results(
                schema_id=schema_id, created_after=created_after, page_size=limit, cursor=cursor
            )
            items = [readback.summarize_record(record) for record in page["items"]]
            response = {
                "source": {"mode": "schema", "schema_id": schema_id, "created_after": created_after}
            }
            response["next_token"] = page["next_token"]
            next_step = readback.pending_next_step(readback.state_counts(items)[readback.PENDING])
        elif result_ids is not None:
            fetched = await self.service.get_assay_results(result_ids)
            items = [readback.summarize_record(record) for record in fetched["items"]]
            not_found = fetched["not_found"]
            response = {"source": {"mode": "ids", "requested": len(items) + len(not_found)}}
            next_step = readback.pending_next_step(readback.state_counts(items)[readback.PENDING])
        elif entry is not None:
            resolved = await self.service.resolve_entry(entry)
            entry_id = resolved["entry_id"]
            known = await self._entry_readback(entry_id)
            items, not_found, plans = known["items"], known["not_found"], known["plans"]
            names = known["names"]
            web_url = resolved.get("web_url") or known["web_url"]
            response = {
                "source": {
                    "mode": "entry",
                    "entry_id": entry_id,
                    "display_id": resolved.get("display_id"),
                    "web_url": web_url,
                },
                "plans": plans,
            }
            if plans:
                next_step = readback.pending_next_step(
                    readback.state_counts(items)[readback.PENDING],
                    entry_id=entry_id,
                    table_names=self._pending_tables(items, names),
                    web_url=web_url,
                )
            else:
                next_step = readback.NO_ENTRY_PLANS_NEXT_STEP
        else:
            assert plan_id is not None
            plan, journal = self._readback_plan(plan_id)
            submitted = await self._submitted_batches(journal)
            names = {
                batch["table_id"]: batch["table_name"] for batch in readback.planned_batches(plan)
            }
            items, not_found = await self._read_records(
                {
                    record_id: batch.get("table_id")
                    for batch in submitted
                    for record_id in batch["record_ids"]
                }
            )
            response = {
                "source": {
                    "mode": "plan",
                    "plan_id": plan.id,
                    "entry_id": plan.entry_id,
                    "web_url": plan_entry_web_url(plan),
                },
                "batches": [
                    {
                        key: batch[key]
                        for key in (
                            "batch_index",
                            "table_id",
                            "task_id",
                            "task_status",
                            "remote_task_error",
                        )
                        if key in batch
                    }
                    | {"record_count": len(batch["record_ids"])}
                    for batch in submitted
                ],
            }
            next_step = readback.pending_next_step(
                len([item for item in items if item["state"] == readback.PENDING]),
                entry_id=plan.entry_id,
                table_names=self._pending_tables(items, names),
                web_url=plan_entry_web_url(plan),
            )
        ordered = readback.sort_by_state(items)
        counts = readback.state_counts(items)
        response.update(
            {
                "counts": {"read": len(items), **counts, "not_found": len(not_found)},
                "items": ordered[:limit],
                "returned": min(len(ordered), limit),
                "truncated": len(ordered) > limit,
                "needs_accept": [
                    item["id"] for item in ordered if item["state"] == readback.PENDING
                ][:limit],
                "not_found": not_found[:limit],
                "not_found_details": [assay_result_lookup(item) for item in not_found[:limit]],
                "next_step": next_step,
            }
        )
        return response

    async def verify_results(self, plan_id: str, *, sample: int = 20) -> dict[str, Any]:
        """Read back a result upload plan's records and diff them against the plan.

        Planned fields are compared by system name. Mismatched, missing and extra rows
        are counted in full and listed up to ``sample`` each. Nothing is written.
        """
        sample = self._readback_limit(sample, "sample", 200)
        plan, journal = self._readback_plan(plan_id)
        batches = readback.planned_batches(plan, readback.uploaded_blob_ids(journal))
        submitted = {
            batch["batch_index"]: batch for batch in await self._submitted_batches(journal)
        }
        if len(batches) == 1 and len(submitted) == 1 and 0 not in submitted:
            submitted = {0: next(iter(submitted.values()))}
        all_ids = [record_id for batch in submitted.values() for record_id in batch["record_ids"]]
        records: dict[str, dict[str, Any]] = {}
        not_found: set[str] = set()
        if all_ids:
            fetched = await self.service.get_assay_results(all_ids)
            records = {
                str(key): record
                for record in fetched["items"]
                for key in (record.get("id"), record.get("v3Id"))
                if isinstance(key, str)
            }
            not_found = set(fetched["not_found"])
        units: list[_VerifyUnit] = []
        for index, batch in enumerate(batches):
            task = submitted.get(index)
            if task is None:
                reason = self._unsubmitted_reason(journal, index)
                found: list[dict[str, Any]] = []
            else:
                found = [
                    records[item] for item in dict.fromkeys(task["record_ids"]) if item in records
                ]
                if any(item in not_found for item in task["record_ids"]):
                    reason = "result_not_found"
                elif not task["record_ids"]:
                    reason = self._no_ids_reason(task)
                else:
                    reason = "no_matching_result"
            diff = readback.diff_batch(batch, found, missing_reason=reason)
            units.append(
                (
                    batch,
                    found,
                    diff,
                    {"task_id": task["task_id"], "task_status": task.get("task_status")}
                    if task
                    else {"submission": reason},
                )
            )
        return self._verify_report(
            units,
            head={
                "plan_id": plan.id,
                "entry_id": plan.entry_id,
                "web_url": plan_entry_web_url(plan),
            },
            not_found=len(not_found),
            sample=sample,
        )

    async def verify_payload(
        self,
        payload: Any,
        *,
        result_ids: list[str] | None = None,
        created_after: str | None = None,
        created_before: str | None = None,
        entry: str | None = None,
        sample: int = 20,
    ) -> dict[str, Any]:
        """Diff an SDK bulk-create payload against the records Benchling holds.

        The payload is a V2 bulk-create body, a bare list of result objects, or a list of
        bodies. Records are found by ``result_ids`` (V2 UUIDs or V3 ``result_…`` ids,
        mixed) or, per payload ``schemaId``, by V2 ``GET /assay-results`` created at or
        after ``created_after`` (and at or before ``created_before``). ``entry`` drops
        accepted records of another entry; pending records have no entry and are kept.
        Rows with a client ``id`` are paired with that record first. The diff and report
        are the ones ``verify_results`` uses. Nothing is written or saved.
        """
        sample = self._readback_limit(sample, "sample", 200)
        batches = readback.payload_batches(payload)
        if (result_ids is None) == (created_after is None):
            raise ValidationError("Pass exactly one of result_ids or created_after with a payload")
        if created_before is not None and created_after is None:
            raise ValidationError("created_before applies only with created_after")
        entry_id: str | None = None
        web_url: str | None = None
        if entry is not None:
            if not isinstance(entry, str) or not entry.strip():
                raise ValidationError("entry must be an entry ID, display ID, or URL")
            resolved = await self.service.resolve_entry(entry)
            entry_id, web_url = resolved["entry_id"], resolved.get("web_url")
        not_found: list[str] = []
        warnings: list[str] = []
        lookup: dict[str, Any]
        if result_ids is not None:
            fetched = await self.service.get_assay_results(result_ids)
            records, not_found = list(fetched["items"]), list(fetched["not_found"])
            lookup = {"lookup": "ids", "requested": len(records) + len(not_found)}
        else:
            assert created_after is not None
            records = []
            for schema_id in dict.fromkeys(str(batch["schema_id"]) for batch in batches):
                cursor, pages = None, 0
                while pages < _PAYLOAD_PAGES:
                    page = await self.service.list_assay_results(
                        schema_id=schema_id,
                        created_after=created_after,
                        created_before=created_before,
                        page_size=100,
                        cursor=cursor,
                    )
                    pages += 1
                    records.extend(page["items"])
                    cursor = page["next_token"]
                    if not cursor:
                        break
                if cursor:
                    warnings.append(
                        f"Only the first {_PAYLOAD_PAGES * 100} results of schema {schema_id} "
                        "in the time window were read; narrow it with created_before"
                    )
            lookup = {
                "lookup": "created_window",
                "created_after": created_after,
                "created_before": created_before,
            }
        kept = [
            record
            for record in records
            if entry_id is None or not record.get("entryId") or record.get("entryId") == entry_id
        ]
        by_schema: dict[str | None, list[dict[str, Any]]] = {}
        for record in kept:
            by_schema.setdefault(readback.record_schema_id(record), []).append(record)
        reason = "result_not_found" if not_found else "no_matching_result"
        units: list[_VerifyUnit] = []
        for batch in batches:
            found = by_schema.pop(batch["schema_id"], [])
            diff = readback.diff_payload_batch(batch, found, missing_reason=reason)
            units.append((batch, found, diff, {"schema_id": batch["schema_id"]}))
        for other_schema, found in by_schema.items():
            batch = {
                "table_id": None,
                "schema_id": other_schema,
                "table_name": None,
                "records": [],
                "plan_row_offset": 0,
            }
            diff = readback.diff_batch(batch, found, missing_reason=reason)
            units.append((batch, found, diff, {"schema_id": other_schema}))
        source: dict[str, Any] = {
            "mode": "payload",
            "records": sum(len(batch["records"]) for batch in batches),
            "schemas": [
                {
                    "schema_id": batch["schema_id"],
                    "table_id": batch["table_id"],
                    "records": len(batch["records"]),
                    "fields": sorted(
                        {name for record in batch["records"] for name in record["fields"]}
                    ),
                }
                for batch in batches
            ],
            **lookup,
            "entry_id": entry_id,
            "other_entry_dropped": len(records) - len(kept),
            "not_found": not_found[:sample],
            "warnings": warnings,
        }
        return self._verify_report(
            units,
            head={"source": source, "plan_id": None, "entry_id": entry_id, "web_url": web_url},
            not_found=len(not_found),
            sample=sample,
        )

    @staticmethod
    def _verify_report(
        units: list[_VerifyUnit],
        *,
        head: dict[str, Any],
        not_found: int,
        sample: int,
    ) -> dict[str, Any]:
        """Assemble the verify report from each batch, its fetched records and its diff.

        ``head`` leads the report and gives the ``entry_id`` and ``web_url`` used in
        ``next_step``. Each unit's last item is added to that batch's report.
        """
        totals: dict[str, list[dict[str, Any]]] = {
            "matched": [],
            "mismatched": [],
            "missing": [],
            "extra": [],
        }
        fetched_states: list[dict[str, Any]] = []
        batch_reports = []
        batches = [batch for batch, _found, _diff, _extra in units]
        for batch, found, diff, extra in units:
            for key, rows in diff.items():
                totals[key].extend(rows)
            fetched_states.extend({"state": readback.result_state(record)} for record in found)
            batch_reports.append(
                {
                    "table_id": batch["table_id"],
                    "planned": len(batch["records"]),
                    "fetched": len(found),
                    **{key: len(rows) for key, rows in diff.items()},
                    **extra,
                }
            )
        states = readback.state_counts(fetched_states)
        paired = totals["matched"] + totals["mismatched"] + totals["extra"]
        pending_rows = [row for row in paired if row["state"] == readback.PENDING]
        pending_tables = [
            str(name)
            for name in dict.fromkeys(
                next(
                    (b["table_name"] for b in batches if b["table_id"] == row["table_id"]),
                    row["table_id"],
                )
                for row in pending_rows
            )
            if name is not None
        ]
        planned_count = sum(len(batch["records"]) for batch in batches)
        ok = not (totals["mismatched"] or totals["missing"] or totals["extra"])
        return {
            **head,
            "ok": ok,
            "summary": (
                f"{len(totals['matched'])} of {planned_count} planned rows match; "
                f"{len(totals['mismatched'])} mismatched, {len(totals['missing'])} missing, "
                f"{len(totals['extra'])} extra; {states[readback.PENDING]} pending acceptance"
            ),
            "counts": {
                "planned": planned_count,
                "fetched": len(fetched_states),
                **{key: len(rows) for key, rows in totals.items()},
                **states,
                "not_found": not_found,
            },
            "sample_limit": sample,
            "mismatched": totals["mismatched"][:sample],
            "missing": totals["missing"][:sample],
            "extra": totals["extra"][:sample],
            "needs_accept": [
                {
                    key: row.get(key)
                    for key in ("table_id", "plan_row_index", "result_id", "v2_id", "v3_id")
                }
                for row in pending_rows
            ][:sample],
            "batches": batch_reports,
            "next_step": readback.pending_next_step(
                len(pending_rows),
                entry_id=head.get("entry_id"),
                table_names=pending_tables,
                web_url=head.get("web_url"),
            ),
        }

    def _result_tasks(self, journal: ChangeJournal) -> list[dict[str, Any]]:
        """Find task submissions in append-only events, with support for older journals."""
        tasks = []
        prior_result = journal.result or {}
        for index, event in enumerate(journal.events):
            if (
                not _is_result_creation_event(event.stage)
                or event.status != "succeeded"
                or not event.details.get("task_id")
            ):
                continue
            tasks.append(
                {
                    **event.details,
                    "batch_index": event.details.get("batch_index", index),
                    "table_id": event.details.get("table_id", prior_result.get("table_id")),
                    "client_record_ids": event.details.get(
                        "client_record_ids", prior_result.get("client_record_ids", [])
                    ),
                    "plan_row_offset": event.details.get("plan_row_offset", 0),
                }
            )
        if tasks:
            return tasks
        task_id = (journal.result or {}).get("task_id")
        if task_id:
            return [
                {
                    "task_id": task_id,
                    "plan_row_offset": (journal.result or {}).get("plan_row_offset", 0),
                    "table_id": (journal.result or {}).get("table_id"),
                    "batch_index": 0,
                    "client_record_ids": (journal.result or {}).get("client_record_ids", []),
                    "row_count": len((journal.result or {}).get("client_record_ids", [])),
                }
            ]
        batches = (journal.result or {}).get("batches")
        if isinstance(batches, list):
            return [batch for batch in batches if isinstance(batch, dict) and batch.get("task_id")]
        return []

    async def _poll_result_task(
        self, task: dict[str, Any], *, wait: bool, deadline: float
    ) -> dict[str, Any]:
        """Read a task until it is terminal or the caller's wait deadline expires."""
        task_id = task["task_id"]
        result: dict[str, Any] = {
            "batch_index": task.get("batch_index", 0),
            "table_id": task.get("table_id"),
            "task_id": task_id,
        }
        delay = 0.25
        try:
            while True:
                remaining = deadline - asyncio.get_running_loop().time()
                if wait and remaining <= 0:
                    result["timed_out"] = True
                    result["result_state"] = "submission_running"
                    break
                try:
                    request = self.service.get_task(task_id)
                    remote = (
                        await asyncio.wait_for(request, timeout=remaining)
                        if wait
                        else await request
                    )
                except TimeoutError:
                    result["timed_out"] = True
                    result["result_state"] = "submission_running"
                    break
                remote_status: Any = remote.get("status")
                result.update(
                    {
                        "remote_task": remote,
                        "task_status": remote_status,
                        "result_state": {
                            "RUNNING": "submission_running",
                            "FAILED": "submission_failed",
                            "SUCCEEDED": "pending_integration",
                        }.get(remote_status, "unknown"),
                    }
                )
                if not wait or remote_status != "RUNNING":
                    break
                remaining = deadline - asyncio.get_running_loop().time()
                if remaining <= 0:
                    result["timed_out"] = True
                    break
                await asyncio.sleep(min(delay, remaining))
                delay = min(delay * 2, 5.0)
        except BenchlingAgentError as exc:
            result["remote_task_error"] = {
                "code": exc.code,
                "message": exc.message,
                "retryable": exc.retryable,
                "details": exc.details,
            }
            return result
        records = (result.get("remote_task", {}).get("response") or {}).get("assayResults", [])
        result["record_ids"] = (
            [
                record.get("id")
                for record in records
                if isinstance(record, dict) and record.get("id")
            ]
            if isinstance(records, list)
            else []
        )
        result["record_v3_ids"] = (
            [
                record.get("v3Id")
                for record in records
                if isinstance(record, dict) and record.get("v3Id")
            ]
            if isinstance(records, list)
            else []
        )
        if result.get("task_status") == "FAILED":
            row_errors, unmatched_errors = self._map_result_errors(
                result["remote_task"].get("errors"),
                task.get("client_record_ids", []),
                task.get("plan_row_offset", 0),
            )
            result["row_errors"] = row_errors
            result["unmatched_errors"] = unmatched_errors
        return result

    def _map_result_errors(
        self, errors: Any, client_record_ids: Any, plan_row_offset: Any
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        """Map only the documented bulk-task ``index`` field; preserve other raw errors."""
        ids = client_record_ids if isinstance(client_record_ids, list) else []
        if isinstance(errors, list):
            entries = errors
        elif isinstance(errors, dict):
            entries = [errors]
        else:
            entries = [] if errors is None else [errors]
        mapped = []
        unmatched = []
        for error in entries:
            index = error.get("index") if isinstance(error, dict) else None
            if isinstance(index, int) and not isinstance(index, bool) and 0 <= index < len(ids):
                mapped.append(
                    {
                        "table_id": None,
                        "row_index": index,
                        "plan_row_index": plan_row_offset + index
                        if isinstance(plan_row_offset, int)
                        and not isinstance(plan_row_offset, bool)
                        else index,
                        "client_record_id": ids[index],
                        "error": error,
                    }
                )
            else:
                unmatched.append(
                    {
                        "error": error,
                        "reason": "Task error has no usable input row index",
                    }
                )
        return mapped, unmatched

    def _row_errors_by_table(self, results: list[dict[str, Any]]) -> dict[str, Any]:
        """Add each table ID to row-level errors and report unmatched task errors."""
        rows: list[dict[str, Any]] = []
        unmatched: list[dict[str, Any]] = []
        for result in results:
            table_id = result.get("table_id")
            rows.extend({**error, "table_id": table_id} for error in result.get("row_errors", []))
            unmatched.extend(
                {**error, "table_id": table_id} for error in result.get("unmatched_errors", [])
            )
        return {"row_errors": rows, "unmatched_row_errors": unmatched}
