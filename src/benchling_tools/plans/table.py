"""Unstructured table edit planning."""

from __future__ import annotations

from copy import deepcopy
from typing import Any

from ..errors import (
    ConflictError,
    UnsupportedError,
    ValidationError,
)
from ..models import (
    ChangeJournal,
    ChangePlan,
)
from ..services import entry_version
from .base import PlannerBase, PlanParts, write_event_details

_EVALUATION_WARNING = (
    "Updating a cell or appending rows resets table evaluation until "
    "Benchling evaluates the table again"
)


class TablePlanning(PlannerBase):
    """Planning and applying of unstructured table cell edits and row inserts."""

    async def _plan_table(self, entry_id: str, request: dict[str, Any]) -> PlanParts:
        part_id = request.get("table_part_id")
        if not isinstance(part_id, str):
            raise ValidationError("Table plan requires table_part_id")
        target = await self.service.get_table_for_entry(entry_id, part_id)
        table = target["table"]
        content = deepcopy(table.get("content") or {})
        if "UnstructuredTable" not in str(content.get("__typename", "")):
            raise UnsupportedError("Only unstructured tables can be edited")
        version_id = entry_version(table)
        before = {
            "table_part_id": part_id,
            "version_id": version_id,
            "content": content,
        }
        after_content = self._apply_table_operation(content, request)
        after = {
            "table_part_id": part_id,
            "version_id": "changes_on_apply",
            "content": after_content,
        }
        return (
            before,
            after,
            {"entry_version": version_id, "entry_id": entry_id},
            ["server-enforced document version check"],
            [_EVALUATION_WARNING],
        )

    def _apply_table_operation(
        self, content: dict[str, Any], request: dict[str, Any]
    ) -> dict[str, Any]:
        """Return a copy of content with the requested cell edit or row insert applied."""
        operation = request.get("operation")
        if operation == "set_cells":
            return self._table_set_cells(deepcopy(content), request)
        if operation == "append_rows":
            return self._table_append_rows(deepcopy(content), request)
        raise ValidationError(
            "Unknown table operation",
            details={"supported": ["set_cells", "append_rows"]},
        )

    def _table_set_cells(
        self, after_content: dict[str, Any], request: dict[str, Any]
    ) -> dict[str, Any]:
        """Apply validated text updates to individual cells."""
        updates = request.get("updates")
        if not isinstance(updates, list) or not updates:
            raise ValidationError("set_cells requires non-empty updates")
        rows = after_content.get("rows") or []
        seen_coordinates: set[tuple[int, int]] = set()
        for update in updates:
            if (
                not isinstance(update, dict)
                or "value" not in update
                or isinstance(update.get("row"), bool)
                or not isinstance(update.get("row"), int)
                or isinstance(update.get("column"), bool)
                or not isinstance(update.get("column"), int)
                or update["row"] < 0
                or update["column"] < 0
            ):
                raise ValidationError(
                    "Each cell update requires nonnegative integer row and column coordinates and a value",
                    details={"update": update},
                )
            coordinate = (update["row"], update["column"])
            if coordinate in seen_coordinates:
                raise ValidationError(
                    "A table plan cannot update the same cell twice",
                    details={"row": coordinate[0], "column": coordinate[1]},
                )
            seen_coordinates.add(coordinate)
            try:
                row = update["row"]
                column = update["column"]
                cell = rows[row]["cells"][column]
            except (KeyError, TypeError, IndexError) as exc:
                raise ValidationError(
                    "Cell coordinate is outside the table",
                    details={"update": update},
                ) from exc
            if cell.get("mergeRangeParent"):
                raise UnsupportedError("Cannot write directly to a merged child cell")
            cell["inputValue"] = {
                "type": "text_value",
                "value": str(update.get("value", "")),
            }
            cell.pop("evaluationValue", None)
            cell.pop("evaluatedValue", None)
        after_content["isEvaluated"] = False
        return after_content

    def _table_append_rows(
        self, after_content: dict[str, Any], request: dict[str, Any]
    ) -> dict[str, Any]:
        """Append normalized rows to the end of the table."""
        rows = request.get("rows")
        if not isinstance(rows, list) or not rows:
            raise ValidationError("append_rows requires non-empty rows")
        expected = len(after_content.get("columnDefinitions") or [])
        normalized = [self._normalize_row(row, expected) for row in rows]
        after_content.setdefault("rows", []).extend(normalized)
        after_content["isEvaluated"] = False
        return after_content

    async def _apply_table(self, plan: ChangePlan, journal: ChangeJournal) -> dict[str, Any]:
        entry_id = self._entry_id(plan)
        part_id = plan.after["table_part_id"]
        current = await self.service.get_table(part_id)
        version = entry_version(current)
        expected = plan.preconditions.get("entry_version")
        self._expect(version, expected, "entry version")
        operation = plan.request.get("operation")
        if operation == "set_cells":
            mutation = self._cell_updates_mutation(plan, version, part_id)
            stage = "update_table_cells"
        elif operation == "append_rows":
            mutation = self._inserted_rows_mutation(plan, version, part_id)
            stage = "insert_table_rows"
        else:
            raise ValidationError(
                "Unknown table operation",
                details={"supported": ["set_cells", "append_rows"]},
            )
        try:
            response = await self.api.request(
                "PATCH", f"/entry/{entry_id}", json={"partsMutation": mutation}
            )
        except ConflictError as exc:
            raise ConflictError(
                "Entry version changed after the plan was created",
                details={"expected": expected, "status": exc.details.get("status")},
            ) from exc
        self._event(journal, stage, "succeeded", write_event_details(response))
        return {
            "table_part_id": part_id,
            "operation": operation,
            "response": response,
            "evaluated_values_cleared": True,
        }

    def _cell_updates_mutation(
        self, plan: ChangePlan, version: str, part_id: str
    ) -> dict[str, Any]:
        """Build an update_table_cells body for the cells named in the plan."""
        return {
            "operationType": "update_table_cells",
            "version": version,
            "documentPartId": part_id,
            "cellUpdates": [
                {
                    "rowIndex": update["row"],
                    "columnIndex": update["column"],
                    "inputValue": {
                        "type": "text_value",
                        "value": str(update.get("value", "")),
                    },
                }
                for update in plan.request["updates"]
            ],
        }

    def _inserted_rows_mutation(
        self, plan: ChangePlan, version: str, part_id: str
    ) -> dict[str, Any]:
        """Build an insert_table_rows body for the rows the plan appends."""
        before_count = len(plan.before["content"]["rows"])
        added = plan.after["content"]["rows"][before_count:]
        rows = []
        for row in added:
            rows.append(
                {
                    "cells": [
                        {
                            "columnIndex": index,
                            "inputValue": {
                                "type": "text_value",
                                "value": str(cell["inputValue"]["value"]),
                            },
                        }
                        for index, cell in enumerate(row["cells"])
                    ]
                }
            )
        return {
            "operationType": "insert_table_rows",
            "version": version,
            "documentPartId": part_id,
            "position": "end_of_table",
            "rows": rows,
        }

    def _normalize_row(self, row: Any, expected_columns: int) -> dict[str, Any]:
        if isinstance(row, list):
            cells = [{"inputValue": {"type": "text_value", "value": str(value)}} for value in row]
            row = {"cells": cells}
        if not isinstance(row, dict) or not isinstance(row.get("cells"), list):
            raise ValidationError("Each table row must contain cells")
        if expected_columns and len(row["cells"]) != expected_columns:
            raise ValidationError(
                "Table row has the wrong number of cells",
                details={"expected": expected_columns, "actual": len(row["cells"])},
            )
        normalized = deepcopy(row)
        for index, cell in enumerate(normalized["cells"]):
            if not isinstance(cell, dict):
                normalized["cells"][index] = {
                    "inputValue": {"type": "text_value", "value": str(cell)}
                }
            elif "inputValue" not in cell:
                cell["inputValue"] = {"type": "text_value", "value": ""}
        return normalized
