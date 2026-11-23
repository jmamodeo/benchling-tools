"""Document-part change planning."""

from __future__ import annotations

from typing import Any

from ..errors import (
    BenchlingAgentError,
    UnsupportedError,
    ValidationError,
)
from ..models import (
    ChangeJournal,
    ChangePlan,
)
from ..services import (
    entry_version,
    is_section,
    part_content,
    part_type,
)
from .base import PlannerBase, PlanParts, write_event_details


class DocumentPlanning(PlannerBase):
    """Planning and applying of document-part changes."""

    async def _plan_document(self, entry_id: str, request: dict[str, Any]) -> PlanParts:
        snapshot = await self.service.get_entry(entry_id, full=True)
        operation = request.get("operation")
        parts = snapshot["parts"]
        by_id = {part.get("id"): part for part in parts}
        version_id = snapshot["version_id"]
        if operation == "insert":
            before, after = self._plan_insert(request, version_id, by_id)
        elif operation == "delete":
            before, after = self._plan_delete(request, version_id, parts)
        elif operation == "move":
            before, after = self._plan_move(request, version_id, parts, by_id)
        elif operation == "replace_text":
            before, after = self._plan_replace_text(request, version_id, by_id)
        else:
            raise ValidationError(
                "Unknown document operation",
                details={"supported": ["insert", "delete", "move", "replace_text"]},
            )
        return (
            before,
            after,
            {
                "entry_version": version_id,
                "part_ids": [part.get("id") for part in parts if part.get("id")],
            },
            ["server-enforced document version check"],
            (
                ["Text replacement requires two versioned requests and can partially complete"]
                if operation == "replace_text"
                else []
            ),
        )

    def _plan_insert(
        self, request: dict[str, Any], version_id: str, by_id: dict[Any, Any]
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        """Validate an insert and return its before/after states."""
        new_parts = request.get("parts")
        position = request.get("position", "end_of_document")
        if not isinstance(new_parts, list) or not new_parts:
            raise ValidationError("Document insert requires a non-empty parts list")
        if any(
            not isinstance(part, dict) or not isinstance(part.get("contentType"), str)
            for part in new_parts
        ):
            raise ValidationError("Every inserted document part requires a contentType")
        if position not in {"beginning_of_document", "end_of_document"} and position not in by_id:
            raise ValidationError("Insert position is not a document part")
        before = {"version_id": version_id, "position": position, "parts": []}
        after = {"version_id": "changes_on_apply", "position": position, "parts": new_parts}
        return before, after

    def _plan_delete(
        self, request: dict[str, Any], version_id: str, parts: list[dict[str, Any]]
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        """Validate a part-range delete and return its before/after states."""
        first = request.get("from_part_id")
        last = request.get("to_part_id", first)
        selected = self._part_range(parts, first, last)
        before = {"version_id": version_id, "parts": selected}
        after = {"version_id": "changes_on_apply", "parts": []}
        return before, after

    def _plan_move(
        self,
        request: dict[str, Any],
        version_id: str,
        parts: list[dict[str, Any]],
        by_id: dict[Any, Any],
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        """Validate a part-range move and return its before/after states."""
        first = request.get("from_part_id")
        last = request.get("to_part_id", first)
        selected = self._part_range(parts, first, last)
        destination = request.get("position")
        if (
            destination not in {"beginning_of_document", "end_of_document"}
            and destination not in by_id
        ):
            raise ValidationError("Move destination is not a document part")
        if destination in {part.get("id") for part in selected}:
            raise ValidationError("Move destination cannot be inside the source range")
        reordered = self._parts_after_move(parts, selected, destination)
        if reordered and not is_section(reordered[0]):
            raise ValidationError(
                "Entry must start with a section",
                details={
                    "position": destination,
                    "first_part_id": reordered[0].get("id"),
                    "first_type": part_type(reordered[0]),
                },
            )
        before = {"version_id": version_id, "parts": selected}
        after = {"version_id": "changes_on_apply", "position": destination, "parts": selected}
        return before, after

    def _plan_replace_text(
        self, request: dict[str, Any], version_id: str, by_id: dict[Any, Any]
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        """Validate a text replacement and return its before/after states."""
        original = by_id.get(request.get("part_id"))
        if not original:
            raise ValidationError("Text replacement target was not found")
        if part_type(original).split(".")[-1] not in {"TextDocumentContent", "DocumentPartText"}:
            raise UnsupportedError("replace_text only supports text document parts")
        if not isinstance(request.get("text"), str):
            raise ValidationError("replace_text requires text")
        before = {"version_id": version_id, "part": original}
        content = part_content(original)
        replacement = {
            "contentType": "text",
            "text": request["text"],
            "formats": request.get("formats", content.get("formats", [])),
            "links": request.get("links", content.get("links", [])),
            "mentions": request.get("mentions", content.get("mentions", [])),
            "indentation": request.get("indentation", content.get("indentation", 0)),
        }
        after = {"version_id": "changes_on_apply", "part": replacement}
        return before, after

    async def _apply_document(self, plan: ChangePlan, journal: ChangeJournal) -> dict[str, Any]:
        entry_id = self._entry_id(plan)
        current = await self.api.request("GET", f"/entry/{entry_id}")
        version = entry_version(current)
        self._expect(version, plan.preconditions["entry_version"], "entry version")
        request = plan.request
        operation = request["operation"]
        if operation == "replace_text":
            return await self._apply_replace_text(plan, journal, version)

        mutation = self._parts_mutation(operation, request, version)
        response = await self.api.request(
            "PATCH", f"/entry/{entry_id}", json={"partsMutation": mutation}
        )
        self._event(journal, operation, "succeeded", write_event_details(response))
        if operation == "insert":
            discovered = await self._discover_new_part_ids(
                entry_id, plan.preconditions.get("part_ids", [])
            )
            changed_part_ids = discovered["ids"]
        else:
            discovered = {"complete": True, "ids": []}
            changed_part_ids = [
                part.get("id") for part in plan.before.get("parts", []) if part.get("id")
            ]
        return {
            "operation": operation,
            "response": response,
            "changed_part_ids": changed_part_ids,
            "part_id_discovery": discovered,
        }

    async def _apply_replace_text(
        self, plan: ChangePlan, journal: ChangeJournal, version: str
    ) -> dict[str, Any]:
        """Replace text by inserting the new part, then deleting the original (two requests)."""
        entry_id = self._entry_id(plan)
        target = plan.request["part_id"]
        insertion = {
            "operationType": "insert",
            "version": version,
            "position": target,
            "parts": [plan.after["part"]],
        }
        first = await self.api.request(
            "PATCH", f"/entry/{entry_id}", json={"partsMutation": insertion}
        )
        self._event(journal, "insert_replacement", "succeeded", write_event_details(first))
        self.repository.save_journal(journal)
        updated = await self.api.request("GET", f"/entry/{entry_id}")
        next_version = entry_version(updated)
        deletion = {
            "operationType": "delete",
            "version": next_version,
            "sourceRange": {"start": target, "end": target},
        }
        second = await self.api.request(
            "PATCH", f"/entry/{entry_id}", json={"partsMutation": deletion}
        )
        self._event(journal, "delete_original", "succeeded", write_event_details(second))
        discovered = await self._discover_new_part_ids(
            entry_id, plan.preconditions.get("part_ids", [])
        )
        return {
            "operations": [first, second],
            "changed_part_ids": [target, *discovered["ids"]],
            "part_id_discovery": discovered,
        }

    def _parts_mutation(
        self, operation: str, request: dict[str, Any], version: str
    ) -> dict[str, Any]:
        """Build the partsMutation payload for an insert, delete or move."""
        if operation == "insert":
            return {
                "operationType": "insert",
                "version": version,
                "position": request.get("position", "end_of_document"),
                "parts": request["parts"],
            }
        if operation == "delete":
            return {
                "operationType": "delete",
                "version": version,
                "sourceRange": {
                    "start": request["from_part_id"],
                    "end": request.get("to_part_id", request["from_part_id"]),
                },
            }
        return {
            "operationType": "move",
            "version": version,
            "sourceRange": {
                "start": request["from_part_id"],
                "end": request.get("to_part_id", request["from_part_id"]),
            },
            "destinationPosition": request["position"],
        }

    def _parts_after_move(
        self,
        parts: list[dict[str, Any]],
        selected: list[dict[str, Any]],
        destination: Any,
    ) -> list[dict[str, Any]]:
        """Document order after a move. A part id inserts the range immediately before that part."""
        selected_ids = {part.get("id") for part in selected}
        remaining = [part for part in parts if part.get("id") not in selected_ids]
        if destination == "beginning_of_document":
            return [*selected, *remaining]
        if destination == "end_of_document":
            return [*remaining, *selected]
        index = next(i for i, part in enumerate(remaining) if part.get("id") == destination)
        return [*remaining[:index], *selected, *remaining[index:]]

    def _part_range(
        self, parts: list[dict[str, Any]], first: Any, last: Any
    ) -> list[dict[str, Any]]:
        ids = [part.get("id") for part in parts]
        try:
            start, end = ids.index(first), ids.index(last)
        except ValueError as exc:
            raise ValidationError("Document range contains an unknown part ID") from exc
        if end < start:
            raise ValidationError("Document range is reversed")
        return parts[start : end + 1]

    async def _discover_new_part_ids(
        self, entry_id: str, previous_ids: list[str]
    ) -> dict[str, Any]:
        try:
            current = [
                item.get("id")
                async for item in self.api.paginate(
                    f"/entry/{entry_id}/parts/items", params={"pageSize": 100}
                )
                if item.get("id")
            ]
            previous = set(previous_ids)
            return {"complete": True, "ids": [item for item in current if item not in previous]}
        except BenchlingAgentError as exc:
            return {
                "complete": False,
                "ids": [],
                "error": {"code": exc.code, "message": exc.message},
            }
        except Exception as exc:
            return {
                "complete": False,
                "ids": [],
                "error": {"code": "local_discovery_error", "type": type(exc).__name__},
            }
