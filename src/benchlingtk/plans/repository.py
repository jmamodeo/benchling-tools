"""Plan persistence: checksummed plan files and apply journals with atomic writes."""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any

from ..config import ConfigStore
from ..errors import (
    ValidationError,
)
from ..models import (
    ChangeJournal,
    ChangePlan,
)
from ..redact import redact

SAFE_ID = re.compile(r"^[0-9a-f]{32}$")


def canonical_json(value: Any) -> str:
    """Serialize deterministically (sorted keys, compact) so checksums are stable."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def checksum_for(plan: dict[str, Any]) -> str:
    """SHA-256 of a plan's canonical JSON, ignoring any existing checksum field."""
    clean = dict(plan)
    clean.pop("checksum", None)
    return hashlib.sha256(canonical_json(clean).encode()).hexdigest()


def json_diff(before: Any, after: Any) -> dict[str, Any]:
    """Return both values and whether they differ; not a structural diff."""
    return {"before": before, "after": after, "changed": before != after}


class PlanRepository:
    """Stores plans and journals under the config directory, keyed by validated plan ID."""

    def __init__(self, config: ConfigStore) -> None:
        self.config = config

    def save_plan(self, plan: ChangePlan) -> None:
        """Write a new plan file; raises ValidationError if the ID already exists."""
        self.config.ensure()
        path = self._plan_path(plan.id)
        if path.exists():
            raise ValidationError("Plan ID already exists")
        self._atomic_write(path, plan.model_dump_json(indent=2, exclude_none=True))

    def load_plan(self, plan_id: str) -> ChangePlan:
        """Load a plan; raises ValidationError if it is unknown or its checksum does not match."""
        path = self._plan_path(plan_id)
        if not path.exists():
            raise ValidationError("Unknown change plan", details={"plan_id": plan_id})
        return self._load_plan_path(path, plan_id)

    def load_plan_file(self, path: Path) -> ChangePlan:
        """Load and verify a plan from an explicit portable file path."""
        try:
            plan = self._load_plan_path(path, path.stem)
        except FileNotFoundError as exc:
            raise ValidationError("Unknown change plan file", details={"path": str(path)}) from exc
        self._validate_id(plan.id)
        return plan

    def _load_plan_path(self, path: Path, plan_id: str) -> ChangePlan:
        plan = ChangePlan.model_validate_json(path.read_text("utf-8"))
        if checksum_for(plan.model_dump(mode="json")) != plan.checksum:
            raise ValidationError(
                "Change plan checksum does not match; the plan cannot be applied",
                details={"plan_id": plan_id},
            )
        return plan

    def load_journal(self, plan_id: str, *, plan_file: Path | None = None) -> ChangeJournal:
        """Load a plan's journal, or a fresh pending one if none was written yet."""
        path = self._journal_path(plan_id)
        if not path.exists() and plan_file is not None:
            candidates = [plan_file.with_name(f"{plan_id}.journal.json")]
            if plan_file.parent.name == "plans":
                candidates.append(plan_file.parent.parent / "journals" / f"{plan_id}.json")
            path = next((candidate for candidate in candidates if candidate.is_file()), path)
        if not path.exists():
            return ChangeJournal(plan_id=plan_id)
        journal = ChangeJournal.model_validate_json(path.read_text("utf-8"))
        if journal.plan_id != plan_id:
            raise ValidationError(
                "Change journal does not match the plan",
                details={"plan_id": plan_id},
            )
        return journal

    def save_journal(self, journal: ChangeJournal) -> None:
        """Atomically write the journal, replacing any previous version.

        Client secrets and access tokens are removed before the file is written.
        """
        self.config.ensure()
        scrubbed = ChangeJournal.model_validate(redact(journal.model_dump(mode="json")))
        journal.events = scrubbed.events
        journal.result = scrubbed.result
        self._atomic_write(self._journal_path(journal.plan_id), journal.model_dump_json(indent=2))

    def _plan_path(self, plan_id: str) -> Path:
        self._validate_id(plan_id)
        return self.config.plans_dir / f"{plan_id}.json"

    def _journal_path(self, plan_id: str) -> Path:
        self._validate_id(plan_id)
        return self.config.journals_dir / f"{plan_id}.json"

    def _validate_id(self, plan_id: str) -> None:
        if not SAFE_ID.fullmatch(plan_id):
            raise ValidationError("Invalid plan ID", details={"plan_id": plan_id})

    def _atomic_write(self, path: Path, content: str) -> None:
        temporary = path.with_suffix(".tmp")
        temporary.write_text(content, "utf-8")
        try:
            temporary.chmod(0o600)
        except OSError:
            pass
        temporary.replace(path)
