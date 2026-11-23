"""Plan and apply updates to existing V3 results from a mapped CSV file.

Assay-result export writes one column per schema field, named with the field's
display name, plus an ``id`` column. This planner requires that mapping to be
explicit. Display names, system names, and field-definition ids are resolved to
field-definition ids before a plan is saved. ``PATCH /result/{id}`` sends those
ids. ``UpdateResultInput`` has no version field, so each result's ``modifiedAt``
is the version precondition.
"""

from __future__ import annotations

import csv
import io
import json
import math
import re
from pathlib import Path
from typing import Any

from ..errors import (
    BenchlingAgentError,
    ConflictError,
    NotFoundError,
    ValidationError,
)
from ..export import _definition_is_link, _field_pairs
from ..models import ChangeJournal, ChangePlan
from ..redact import redact_text
from ..services import field_definition_type, is_attachment_field
from .base import PlanParts, write_event_details
from .blobs import UPLOAD_WARNINGS, BlobUploads, FileStager, stage_field_values
from .fields import FieldNormalization, LinkCache, _resolve_field

MAX_RESULT_UPDATES = 500
_MAX_REPORTED_ERRORS = 50
_FLOAT = re.compile(r"[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?\Z")
_INTEGER = re.compile(r"[+-]?\d+\Z")
FILE_PREFIX = "file:"


def request_is_result_update(request: dict[str, Any]) -> bool:
    """True when the specification is a CSV update rather than a table upload."""
    if request.get("operation") == "update":
        return True
    return any(key in request for key in ("csv_path", "id_column", "column_map"))


class ResultUpdatePlanning(FieldNormalization, BlobUploads):
    """Planning and applying of updates to existing result records."""

    async def _plan_result_updates(self, entry_id: str, request: dict[str, Any]) -> PlanParts:
        """Read the CSV, resolve columns, and preview every field that would change.

        Raises ValidationError when the mapping or a row is unusable. Nothing is written.
        """
        path, id_column, column_map = _update_request(request)
        stager = FileStager(self.repository.config, path.resolve().parent)
        try:
            return await self._plan_csv_updates(
                entry_id, request, path, id_column, column_map, stager
            )
        except BaseException:
            stager.discard()
            raise

    async def _plan_csv_updates(
        self,
        entry_id: str,
        request: dict[str, Any],
        path: Path,
        id_column: str,
        column_map: dict[str, str],
        stager: FileStager,
    ) -> PlanParts:
        headers, rows, problems = _read_csv(path)
        _check_headers(headers, id_column, column_map)
        if len(rows) > MAX_RESULT_UPDATES:
            raise ValidationError(
                "CSV result updates accept at most 500 rows",
                details={"rows": len(rows), "limit": MAX_RESULT_UPDATES},
            )
        problems.extend(_duplicate_ids(rows, id_column))
        records = await self._load_results(rows, id_column, problems)
        bindings = await self._bind_schemas(
            entry_id, records, rows, id_column, column_map, problems
        )
        changes, unchanged = self._row_changes(
            entry_id, rows, id_column, records, bindings, problems, stager
        )
        await self._check_change_links(changes, bindings, problems)
        _raise_problems(problems)
        if not changes:
            raise ValidationError(
                "CSV did not change any mapped field",
                details={"unchanged_rows": unchanged},
            )
        request["operation"] = "update"
        request["csv_path"] = str(path)
        request["id_column"] = id_column
        request["column_map"] = column_map
        request["updates"] = [
            {"id": change["id"], "schemaFields": change["schema_fields"]} for change in changes
        ]
        before_rows = [_preview_before(change) for change in changes]
        after_rows = [_preview_after(change) for change in changes]
        warnings = _update_warnings(headers, id_column, column_map, unchanged)
        after: dict[str, Any] = {"operation": "update", "results": after_rows}
        guarantees = [
            "modifiedAt of each result is checked before any PATCH",
            "mapped columns are sent as field-definition ids",
            "no automatic write retry",
        ]
        if stager.uploads:
            after["uploads"] = stager.uploads
            warnings.extend(UPLOAD_WARNINGS)
            guarantees.append("each local file's size and SHA-256 are checked again before upload")
        return (
            {"operation": "update", "results": before_rows},
            after,
            {
                "operation": "update",
                "entry_id": entry_id,
                "results": [_precondition(change) for change in changes],
            },
            guarantees,
            warnings,
        )

    async def _load_results(
        self,
        rows: list[tuple[int, dict[str, str]]],
        id_column: str,
        problems: list[dict[str, Any]],
    ) -> dict[str, dict[str, Any]]:
        """GET each distinct result id. A 404 is an unknown row, not a failed plan by itself."""
        broken = {problem.get("id") for problem in problems if problem.get("id")}
        wanted: list[str] = []
        for _, cells in rows:
            result_id = cells.get(id_column, "").strip()
            if result_id and result_id not in broken and result_id not in wanted:
                wanted.append(result_id)
        found: dict[str, dict[str, Any]] = {}
        for result_id in wanted:
            try:
                found[result_id] = await self.service.get_result(result_id)
            except NotFoundError:
                found_rows = [
                    number
                    for number, cells in rows
                    if cells.get(id_column, "").strip() == result_id
                ]
                problems.append(
                    {
                        "row": found_rows[0] if found_rows else None,
                        "id": result_id,
                        "message": f"Unknown result {result_id}",
                    }
                )
        return found

    async def _bind_schemas(
        self,
        entry_id: str,
        records: dict[str, dict[str, Any]],
        rows: list[tuple[int, dict[str, str]]],
        id_column: str,
        column_map: dict[str, str],
        problems: list[dict[str, Any]],
    ) -> dict[str, _Binding | None]:
        """Resolve mapped columns once per result schema. Unknown columns are reported here."""
        schema_ids: list[str] = []
        for _, cells in rows:
            result_id = cells.get(id_column, "").strip()
            record = records.get(result_id)
            if record is None or not _can_update(record, entry_id):
                continue
            schema_id = _result_schema_id(record)
            if schema_id and schema_id not in schema_ids:
                schema_ids.append(schema_id)
        bindings: dict[str, _Binding | None] = {}
        for schema_id in schema_ids:
            try:
                loaded = await self.service.get_schema(schema_id)
            except NotFoundError:
                problems.append({"schema_id": schema_id, "message": "Result schema was not found"})
                bindings[schema_id] = None
                continue
            raw = loaded.get("field_definitions")
            definitions = (
                [item for item in raw if isinstance(item, dict)] if isinstance(raw, list) else []
            )
            bindings[schema_id] = _bind_columns(schema_id, definitions, column_map, problems)
        return bindings

    def _row_changes(
        self,
        entry_id: str,
        rows: list[tuple[int, dict[str, str]]],
        id_column: str,
        records: dict[str, dict[str, Any]],
        bindings: dict[str, _Binding | None],
        problems: list[dict[str, Any]],
        stager: FileStager | None = None,
    ) -> tuple[list[dict[str, Any]], list[str]]:
        """Parse each mapped cell. Unchanged cells are omitted from the plan."""
        changes: list[dict[str, Any]] = []
        unchanged: list[str] = []
        seen_ids = {problem.get("id") for problem in problems}
        for number, cells in rows:
            result_id = cells.get(id_column, "").strip()
            if not result_id or result_id in seen_ids:
                continue
            record = records.get(result_id)
            if record is None:
                continue
            refusal = _row_refusal(number, result_id, record, entry_id)
            if refusal is not None:
                problems.append(refusal)
                seen_ids.add(result_id)
                continue
            schema_id = _result_schema_id(record)
            binding = bindings.get(schema_id or "")
            if binding is None:
                continue
            change = self._one_row(number, result_id, record, binding, cells, problems, stager)
            if change is None:
                continue
            if not change["fields"]:
                unchanged.append(result_id)
                continue
            changes.append(change)
        return changes, unchanged

    async def _check_change_links(
        self,
        changes: list[dict[str, Any]],
        bindings: dict[str, _Binding | None],
        problems: list[dict[str, Any]],
    ) -> None:
        """Read every linked item a changed link cell names; report the ones that fail."""
        cache: LinkCache = {}
        for change in changes:
            binding = bindings.get(change["schema_id"] or "")
            if binding is None:
                continue
            for column, definition in binding.columns:
                field_input = change["schema_fields"].get(str(definition["id"]))
                if field_input is None:
                    continue
                for exc in await self._link_value_problems(
                    definition, field_input.get("value"), cache
                ):
                    problems.append(
                        {
                            "row": change["row"],
                            "id": change["id"],
                            "column": column,
                            "message": exc.message,
                            "details": exc.details,
                        }
                    )

    def _one_row(
        self,
        number: int,
        result_id: str,
        record: dict[str, Any],
        binding: _Binding,
        cells: dict[str, str],
        problems: list[dict[str, Any]],
        stager: FileStager | None = None,
    ) -> dict[str, Any] | None:
        """Return the fields that differ, or None when this row has a cell error."""
        pairs = _field_pairs(record, binding.names, binding.link_flags)
        fields: dict[str, dict[str, Any]] = {}
        schema_fields: dict[str, dict[str, Any]] = {}
        observed: dict[str, Any] = {}
        row_errors = 0
        for column, definition in binding.columns:
            try:
                parsed = _parse_cell(definition, cells.get(column, ""), column=column)
                supplied = {str(definition["id"]): {"value": parsed}}
                if stager is not None:
                    stage_field_values(
                        supplied, binding.definitions, stager, result_id=result_id, row=number
                    )
                normalized, _targets, _field_name_map = self._normalize_schema_field_updates(
                    supplied,
                    binding.definitions,
                    address_by="field_definition_id",
                    local_files=stager is not None,
                )
            except BenchlingAgentError as exc:
                row_errors += 1
                item: dict[str, Any] = {
                    "row": number,
                    "id": result_id,
                    "column": column,
                    "message": exc.message,
                }
                if exc.details:
                    item["details"] = exc.details
                problems.append(item)
                continue
            field_id = str(definition["id"])
            system_name = str(definition["systemName"])
            current = _value_for(pairs, definition)
            new_value = normalized[field_id]["value"]
            if _same(definition, current, new_value):
                continue
            snapshot = {
                "field_definition_id": field_id,
                "system_name": system_name,
                "name": definition.get("name"),
                "before": current,
                "after": new_value,
            }
            fields[system_name] = snapshot
            schema_fields[field_id] = normalized[field_id]
            observed[system_name] = current
        if row_errors:
            return None
        return {
            "row": number,
            "id": result_id,
            "modified_at": record.get("modifiedAt"),
            "schema_id": _result_schema_id(record),
            "entry_id": _result_entry_id(record),
            "fields": fields,
            "schema_fields": schema_fields,
            "observed_values": observed,
            "field_definitions": self._field_definition_fingerprints(
                binding.definitions, set(observed)
            ),
        }

    async def _apply_result_updates(
        self, plan: ChangePlan, journal: ChangeJournal, refs: set[str] | None = None
    ) -> dict[str, Any]:
        """Re-check every result, then upload its files and PATCH. A stale version writes nothing."""
        planned = _planned_updates(plan)
        for item in planned:
            await self._recheck(item)
        updated: list[dict[str, Any]] = []
        uploaded: dict[str, dict[str, Any]] = {}
        for item in planned:
            schema_fields = item["schema_fields"]
            if refs:
                schema_fields = await self._link_local_files(schema_fields, refs, journal, uploaded)
            response = await self.api.request(
                "PATCH",
                f"/result/{item['id']}",
                json={"schemaFields": schema_fields},
            )
            recorded = write_event_details(response, result_id=item["id"])
            self._event(journal, "update_result", "succeeded", recorded)
            updated.append({"id": item["id"], "http_status": recorded.get("http_status")})
        result: dict[str, Any] = {"updated": updated, "state": "updated"}
        if uploaded:
            result["blobs"] = list(uploaded.values())
        return result

    async def _recheck(self, item: dict[str, Any]) -> dict[str, Any]:
        """Refuse the plan when this result's version or mapped values moved."""
        result_id = str(item["id"])
        try:
            record = await self.service.get_result(result_id)
        except NotFoundError as exc:
            raise ConflictError(
                "Result was not found when the plan was applied",
                details={"result_id": result_id},
            ) from exc
        label = f"result {result_id}"
        self._expect(record.get("modifiedAt"), item["modified_at"], f"{label} version")
        self._expect(_result_entry_id(record), item["entry_id"], f"{label} entry")
        self._expect(_result_schema_id(record), item["schema_id"], f"{label} schema")
        loaded = await self.service.get_schema(str(item["schema_id"]))
        raw = loaded.get("field_definitions")
        definitions = (
            [entry for entry in raw if isinstance(entry, dict)] if isinstance(raw, list) else []
        )
        observed_values = item["observed_values"]
        self._expect(
            self._field_definition_fingerprints(definitions, set(observed_values)),
            item["field_definitions"],
            f"{label} field definitions",
        )
        names, link_flags = _name_index(definitions)
        pairs = _field_pairs(record, names, link_flags)
        by_system = {
            str(definition["systemName"]): definition
            for definition in definitions
            if definition.get("systemName")
        }
        observed = {
            system: _value_for(pairs, by_system[system])
            for system in observed_values
            if system in by_system
        }
        self._expect(observed, observed_values, f"{label} field values")
        return record


class _Binding:
    """Columns resolved for one result schema."""

    def __init__(
        self, definitions: list[dict[str, Any]], columns: list[tuple[str, dict[str, Any]]]
    ) -> None:
        self.definitions = definitions
        self.columns = columns
        self.names, self.link_flags = _name_index(definitions)


def _update_request(request: dict[str, Any]) -> tuple[Path, str, dict[str, str]]:
    raw_path = request.get("csv_path")
    if not isinstance(raw_path, str) or not raw_path.strip():
        raise ValidationError("csv_path must be a non-empty string")
    path = Path(raw_path.strip()).expanduser()
    if not path.is_file():
        raise ValidationError("CSV file was not found", details={"path": str(path)})
    id_column = request.get("id_column")
    if not isinstance(id_column, str) or not id_column.strip():
        raise ValidationError("id_column must name the CSV column that holds the result id")
    id_column = id_column.strip()
    column_map = _column_map(request.get("column_map"))
    if id_column in column_map:
        raise ValidationError(
            "id_column cannot also be mapped to a field",
            details={"column": id_column},
        )
    return path, id_column, column_map


def _column_map(raw: object) -> dict[str, str]:
    if not isinstance(raw, dict) or not raw:
        raise ValidationError("column_map must map at least one CSV column to a field")
    mapped: dict[str, str] = {}
    for key, field in raw.items():
        if not isinstance(key, str) or not key.strip():
            raise ValidationError("column_map keys must be CSV column names")
        column = key.strip()
        if column in mapped:
            raise ValidationError(
                "column_map names a CSV column more than once",
                details={"column": column},
            )
        if not isinstance(field, str) or not field.strip():
            raise ValidationError(
                "column_map values must be field names or field-definition ids",
                details={"column": column},
            )
        mapped[column] = field.strip()
    return mapped


def _read_csv(
    path: Path,
) -> tuple[list[str], list[tuple[int, dict[str, str]]], list[dict[str, Any]]]:
    try:
        text = path.read_text(encoding="utf-8-sig")
    except UnicodeDecodeError as exc:
        raise ValidationError("CSV must be UTF-8", details={"path": str(path)}) from exc
    except OSError as exc:
        raise ValidationError(
            "Could not read CSV",
            details={"path": str(path), "reason": redact_text(str(exc))},
        ) from exc
    try:
        parsed = list(csv.reader(io.StringIO(text, newline="")))
    except csv.Error as exc:
        raise ValidationError(
            "Could not read CSV",
            details={"path": str(path), "reason": redact_text(str(exc))},
        ) from exc
    if not parsed:
        raise ValidationError("CSV has no header", details={"path": str(path)})
    headers = [cell.strip() for cell in parsed[0]]
    if not any(headers):
        raise ValidationError("CSV has no header", details={"path": str(path)})
    if any(not header for header in headers):
        raise ValidationError("CSV header has an empty column", details={"path": str(path)})
    repeated = sorted({header for header in headers if headers.count(header) > 1})
    if repeated:
        raise ValidationError(
            "CSV header repeats a column",
            details={"path": str(path), "columns": repeated},
        )
    rows: list[tuple[int, dict[str, str]]] = []
    problems: list[dict[str, Any]] = []
    for number, cells in enumerate(parsed[1:], start=2):
        if not any(cell.strip() for cell in cells):
            continue
        if len(cells) > len(headers):
            problems.append({"row": number, "message": "Row has more cells than the header"})
            continue
        padded = cells + [""] * (len(headers) - len(cells))
        rows.append((number, dict(zip(headers, padded, strict=True))))
    if not rows and not problems:
        raise ValidationError("CSV has no result rows", details={"path": str(path)})
    return headers, rows, problems


def _check_headers(headers: list[str], id_column: str, column_map: dict[str, str]) -> None:
    unknown = [column for column in (id_column, *column_map) if column not in headers]
    if unknown:
        raise ValidationError(
            "CSV column mapping names columns that are not in the header",
            details={"unknown_columns": unknown, "columns": headers},
        )


def _duplicate_ids(rows: list[tuple[int, dict[str, str]]], id_column: str) -> list[dict[str, Any]]:
    seen: dict[str, int] = {}
    problems: list[dict[str, Any]] = []
    for number, cells in rows:
        result_id = cells.get(id_column, "").strip()
        if not result_id:
            problems.append({"row": number, "message": "Result id is blank"})
            continue
        if result_id in seen:
            problems.append(
                {
                    "row": number,
                    "id": result_id,
                    "message": f"Result id {result_id} is repeated",
                }
            )
            first = seen[result_id]
            if not any(
                item.get("row") == first and item.get("id") == result_id for item in problems
            ):
                problems.append(
                    {
                        "row": first,
                        "id": result_id,
                        "message": f"Result id {result_id} is repeated",
                    }
                )
            continue
        seen[result_id] = number
    return problems


def _bind_columns(
    schema_id: str,
    definitions: list[dict[str, Any]],
    column_map: dict[str, str],
    problems: list[dict[str, Any]],
) -> _Binding:
    columns: list[tuple[str, dict[str, Any]]] = []
    used: dict[str, str] = {}
    for column, token in column_map.items():
        resolved = _resolve_field(token, definitions)
        if resolved is None:
            problems.append(
                {
                    "schema_id": schema_id,
                    "column": column,
                    "field": token,
                    "message": f"Unknown column {column} does not match a field on {schema_id}",
                }
            )
            continue
        if isinstance(resolved, list):
            problems.append(
                {
                    "schema_id": schema_id,
                    "column": column,
                    "field": token,
                    "field_ids": [str(item.get("id")) for item in resolved],
                    "message": f"Column {column} matches more than one field on {schema_id}",
                }
            )
            continue
        field_id = resolved.get("id")
        system_name = resolved.get("systemName")
        if not isinstance(field_id, str) or not field_id:
            problems.append(
                {
                    "schema_id": schema_id,
                    "column": column,
                    "field": token,
                    "message": f"Column {column} has no field-definition id",
                }
            )
            continue
        if not isinstance(system_name, str) or not system_name:
            problems.append(
                {
                    "schema_id": schema_id,
                    "column": column,
                    "field": token,
                    "message": f"Column {column} has no system name",
                }
            )
            continue
        if field_id in used:
            problems.append(
                {
                    "schema_id": schema_id,
                    "column": column,
                    "field": token,
                    "field_id": field_id,
                    "message": f"Columns {used[field_id]} and {column} map to the same field",
                }
            )
            continue
        used[field_id] = column
        columns.append((column, resolved))
    return _Binding(definitions, columns)


def _name_index(definitions: list[dict[str, Any]]) -> tuple[dict[str, str], dict[str, bool]]:
    """Field-definition id to display name, and whether that id is a link."""
    names: dict[str, str] = {}
    link_flags: dict[str, bool] = {}
    for definition in definitions:
        field_id = definition.get("id")
        if not isinstance(field_id, str) or not field_id:
            continue
        display = definition.get("name")
        if isinstance(display, str) and display.strip() and field_id not in names:
            names[field_id] = display.strip()
        classified = _definition_is_link(definition)
        if classified is not None and field_id not in link_flags:
            link_flags[field_id] = classified
    return names, link_flags


def _can_update(record: dict[str, Any], entry_id: str) -> bool:
    """True when the result is on this entry and has a schema and a modifiedAt version."""
    modified = record.get("modifiedAt")
    return (
        _result_entry_id(record) == entry_id
        and _result_schema_id(record) is not None
        and isinstance(modified, str)
        and bool(modified)
    )


def _row_refusal(
    number: int, result_id: str, record: dict[str, Any], entry_id: str
) -> dict[str, Any] | None:
    if _result_entry_id(record) != entry_id:
        return {
            "row": number,
            "id": result_id,
            "message": (
                "Result is not on the selected entry. Results created through the API stay "
                "pending until a person accepts them in the entry's results table in Benchling "
                "(the plug icon in the table toolbar). Accept them there first, then plan again."
            ),
        }
    if not _result_schema_id(record):
        return {"row": number, "id": result_id, "message": "Result has no schema"}
    modified = record.get("modifiedAt")
    if not isinstance(modified, str) or not modified:
        return {
            "row": number,
            "id": result_id,
            "message": "Result has no modifiedAt version",
        }
    return None


def _parse_cell(definition: dict[str, Any], cell: str, *, column: str) -> Any:
    """Turn one CSV cell into the value ``schemaFields`` will send.

    A blank cell is null. Export writes both null and an empty string as a blank
    cell, so callers compare those two before treating null as a clear.
    """
    is_multi = bool(definition.get("isMulti"))
    if is_attachment_field(definition):
        text = cell.strip()
        if text == "":
            return None
        if is_multi:
            items = _parse_json_array(text, column=column)
            if not all(isinstance(item, str) for item in items):
                raise ValidationError(f"Column {column} must be a JSON array of strings")
            return [_attachment_cell(item) for item in items]
        return _attachment_cell(text)
    field_type = field_definition_type(definition)
    raw = cell if field_type in {"TEXT", "LONG_TEXT"} and not is_multi else cell.strip()
    if raw == "":
        return None
    if is_multi:
        return _parse_json_array(raw, column=column)
    if field_type in {"TEXT", "LONG_TEXT"}:
        return raw
    if field_type == "INTEGER":
        if _INTEGER.fullmatch(raw):
            return int(raw)
        raise ValidationError(f"Column {column} must be an integer")
    if field_type in {"FLOAT", "DECIMAL"}:
        if _FLOAT.fullmatch(raw):
            value = float(raw)
            if math.isfinite(value):
                return value
        raise ValidationError(f"Column {column} must be a number")
    if field_type == "BOOLEAN":
        lowered = raw.casefold()
        if lowered == "true":
            return True
        if lowered == "false":
            return False
        raise ValidationError(f"Column {column} must be true or false")
    if field_type in {"DATE", "DATETIME", "JSON"}:
        if field_type == "JSON":
            return _parse_json(raw, column=column)
        return raw
    if field_type in {"LINK", "DROPDOWN"}:
        return raw
    raise ValidationError(f"Column {column} has a field type that cannot be updated")


def _attachment_cell(text: str) -> Any:
    """A ``file:PATH`` cell asks for a local upload; any other text is a blob ID."""
    if text.startswith(FILE_PREFIX):
        return {"file": text[len(FILE_PREFIX) :]}
    return text


def _parse_json_array(raw: str, *, column: str) -> list[Any]:
    parsed = _parse_json(raw, column=column)
    if not isinstance(parsed, list):
        raise ValidationError(f"Column {column} must be a JSON array")
    return parsed


def _parse_json(raw: str, *, column: str) -> Any:
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValidationError(f"Column {column} must be JSON") from exc


def _same(definition: dict[str, Any], current: Any, new: Any) -> bool:
    """True when the CSV cell does not change the value export would write."""
    if current == new:
        return True
    if new is None and current in (None, ""):
        return True
    if isinstance(current, list) and isinstance(new, list):
        scalar = {**definition, "isMulti": False}
        return len(current) == len(new) and all(
            _same(scalar, left, right) for left, right in zip(current, new, strict=True)
        )
    if isinstance(current, str) and new is not None and not isinstance(new, str):
        try:
            parsed = _parse_cell({**definition, "isMulti": False}, current, column="")
        except ValidationError:
            return False
        if parsed == new:
            return True
        return False
    return False


def _value_for(pairs: dict[str, Any], definition: dict[str, Any]) -> Any:
    """Current scalar for one field, using the same names export writes."""
    display = definition.get("name")
    if isinstance(display, str) and display.strip() in pairs:
        return pairs[display.strip()]
    system_name = definition.get("systemName")
    if isinstance(system_name, str) and system_name in pairs:
        return pairs[system_name]
    field_id = definition.get("id")
    if isinstance(field_id, str) and field_id in pairs:
        return pairs[field_id]
    return None


def _result_entry_id(record: dict[str, Any]) -> str | None:
    entry_id = record.get("entryId")
    if isinstance(entry_id, str) and entry_id:
        return entry_id
    entry = record.get("entry")
    if isinstance(entry, dict):
        nested = entry.get("id")
        if isinstance(nested, str) and nested:
            return nested
    return None


def _result_schema_id(record: dict[str, Any]) -> str | None:
    schema_id = record.get("schemaId")
    if isinstance(schema_id, str) and schema_id:
        return schema_id
    schema = record.get("schema")
    if isinstance(schema, dict):
        nested = schema.get("id")
        if isinstance(nested, str) and nested:
            return nested
    return None


def _preview_before(change: dict[str, Any]) -> dict[str, Any]:
    return {
        "row": change["row"],
        "id": change["id"],
        "modified_at": change["modified_at"],
        "schema_id": change["schema_id"],
        "fields": {
            system: {
                "field_definition_id": field["field_definition_id"],
                "system_name": system,
                "name": field["name"],
                "value": field["before"],
            }
            for system, field in change["fields"].items()
        },
    }


def _preview_after(change: dict[str, Any]) -> dict[str, Any]:
    return {
        "row": change["row"],
        "id": change["id"],
        "schema_id": change["schema_id"],
        "fields": {
            system: {
                "field_definition_id": field["field_definition_id"],
                "system_name": system,
                "name": field["name"],
                "value": field["after"],
            }
            for system, field in change["fields"].items()
        },
        "schema_fields": change["schema_fields"],
    }


def _precondition(change: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": change["id"],
        "modified_at": change["modified_at"],
        "schema_id": change["schema_id"],
        "entry_id": change["entry_id"],
        "observed_values": change["observed_values"],
        "field_definitions": change["field_definitions"],
    }


def _update_warnings(
    headers: list[str],
    id_column: str,
    column_map: dict[str, str],
    unchanged: list[str],
) -> list[str]:
    warnings = [
        "A blank cell clears a value that is present. A blank cell on an empty field is not a change, so an unmodified export round-trips",
        "PATCH /result writes the result directly. It does not submit a notebook table for integration",
    ]
    ignored = [header for header in headers if header not in column_map and header != id_column]
    if ignored:
        warnings.append("Unmapped CSV columns are not written: " + ", ".join(ignored))
    if unchanged:
        shown = ", ".join(unchanged[:20])
        suffix = f" ({len(unchanged)} rows)" if len(unchanged) > 20 else ""
        warnings.append(f"Unchanged rows were omitted{suffix}: {shown}")
    return warnings


def _raise_problems(problems: list[dict[str, Any]]) -> None:
    if not problems:
        return
    details: dict[str, Any] = {
        "errors": problems[:_MAX_REPORTED_ERRORS],
        "error_count": len(problems),
    }
    if len(problems) > _MAX_REPORTED_ERRORS:
        details["truncated"] = True
    raise ValidationError(
        "CSV result update was rejected before anything was written",
        details=details,
    )


def _planned_updates(plan: ChangePlan) -> list[dict[str, Any]]:
    """Pair each previewed result with the precondition checked before its PATCH."""
    after = plan.after.get("results")
    preconditions = plan.preconditions.get("results")
    if not isinstance(after, list) or not after or not isinstance(preconditions, list):
        raise ValidationError("Result update plan is missing its results")
    by_id = {
        str(item["id"]): item
        for item in preconditions
        if isinstance(item, dict) and isinstance(item.get("id"), str)
    }
    planned: list[dict[str, Any]] = []
    for item in after:
        if not isinstance(item, dict) or not isinstance(item.get("id"), str):
            raise ValidationError("Result update plan is missing its results")
        precondition = by_id.get(item["id"])
        schema_fields = item.get("schema_fields")
        if precondition is None or not isinstance(schema_fields, dict) or not schema_fields:
            raise ValidationError("Result update plan is missing its results")
        planned.append({**precondition, "schema_fields": schema_fields})
    if len(planned) != len(preconditions):
        raise ValidationError("Result update plan is missing its results")
    return planned
