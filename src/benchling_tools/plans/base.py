"""Shared base class for the per-family planners."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from ..api import BenchlingAPI
from ..errors import (
    ConflictError,
    ValidationError,
)
from ..models import (
    ChangeJournal,
    ChangePlan,
    JournalEvent,
)
from ..services import (
    BenchlingService,
)
from .repository import PlanRepository


def write_event_details(response: dict[str, Any], **extra: Any) -> dict[str, Any]:
    """Journal payload for one write: the response body and its raw HTTP status."""
    status = getattr(response, "status_code", None)
    details: dict[str, Any] = {"response": dict(response), **extra}
    if isinstance(status, int):
        details["http_status"] = status
    return details


# before, after, preconditions, guarantees, warnings
PlanParts = tuple[dict[str, Any], dict[str, Any], dict[str, Any], list[str], list[str]]


class PlannerBase:
    """Holds the service, API and repository that every planner mixin relies on."""

    def __init__(
        self,
        service: BenchlingService,
        api: BenchlingAPI,
        repository: PlanRepository,
    ) -> None:
        self.service = service
        self.api = api
        self.repository = repository

    def _expect(self, actual: Any, expected: Any, label: str) -> None:
        if actual != expected:
            raise ConflictError(
                f"{label.capitalize()} changed after the plan was created",
                details={"expected": expected, "actual": actual},
            )

    @staticmethod
    def _entry_id(plan: ChangePlan) -> str:
        """Return the existing entry ID, rejecting a missing plan target."""
        if not plan.entry_id:
            raise ValidationError("Entry change plan has no entry ID")
        return plan.entry_id

    def _event(
        self, journal: ChangeJournal, stage: str, status: str, details: dict[str, Any]
    ) -> None:
        journal.events.append(
            JournalEvent(at=datetime.now(UTC), stage=stage, status=status, details=details)
        )
