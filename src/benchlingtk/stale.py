"""Detect results-table cells that disagree with their linked result record.

After a result is updated with ``PATCH /result/{id}``, a notebook results table
can keep showing the old value. Benchling's web app then offers **Re-sync table**.
The recorded V2 and V3 specs have no call that re-syncs a table: the V3
results-table routes (``/document-part-results-table/{id}`` and its
``rows/items``) are GET only, and ``PATCH /entry/{id}`` ``partsMutation`` has no
re-sync operation. So the toolkit only reports a stale cell; re-syncing is done
in the UI.

A column is compared when its ``SchemaFieldColumnDefinition.fieldDefinitionApiId``
names a field the record returns in ``schemaFields`` (matched on
``fieldDefinition.id``). A row is skipped when it is archived or its
``submissionStatus`` is ``UNSUBMITTED`` or ``HAS_UNSUBMITTED_CHANGES``: such a
row may differ because of a person's unsaved edit, and re-syncing it could
discard that edit. ``SUBMITTED``, ``UNEVALUATED`` (the status Benchling reports
on accepted rows of a table not evaluated since the last write) and a missing
status are compared. These reads never write to Benchling.
"""

from __future__ import annotations

import re
from typing import Any

from .errors import AccessError, NotFoundError
from .export import _cell_value, _unwrap_field
from .services import BenchlingService, is_results_table, part_result_schema_id

STALE_CELL_WARNING = (
    "A results-table cell shows a different value from its result record, so the "
    "notebook table is stale. Ask the person to open the table in Benchling and use "
    "Re-sync table. The Benchling API has no re-sync call, so the toolkit cannot do it."
)
_NUMBER = re.compile(r"\s*[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?\s*\Z")
_EMPTY: tuple[Any, ...] = (None, "", [])


def column_fields(columns: object) -> dict[int, tuple[str, dict[str, Any]]]:
    """Column index to (field-definition id, column) for schema-field columns."""
    mapped: dict[int, tuple[str, dict[str, Any]]] = {}
    if not isinstance(columns, list):
        return mapped
    for index, column in enumerate(columns):
        if not isinstance(column, dict):
            continue
        field_id = column.get("fieldDefinitionApiId")
        if isinstance(field_id, str) and field_id.strip():
            mapped[index] = (field_id.strip(), column)
    return mapped


def row_result_id(row: dict[str, Any]) -> str | None:
    result = row.get("result")
    if isinstance(result, dict):
        result_id = result.get("id")
        if isinstance(result_id, str) and result_id:
            return result_id
    return None


_UNSUBMITTED = frozenset({"UNSUBMITTED", "HAS_UNSUBMITTED_CHANGES"})


def row_is_comparable(row: dict[str, Any]) -> bool:
    """False for an archived row or one with unsubmitted changes; True otherwise."""
    return row.get("submissionStatus") not in _UNSUBMITTED and not row.get("isArchived")


def _record_values(record: dict[str, Any]) -> dict[str, list[Any]] | None:
    """Field-definition id to the values that count as the record's value.

    ``None`` when the record has no ``schemaFields`` list, so nothing can be
    compared. A null value may name a linked entity through ``linkedEntityId``;
    that id is accepted too unless it is the result's own id.
    """
    raw = record.get("schemaFields")
    if not isinstance(raw, list):
        return None
    owner = record.get("id")
    values: dict[str, list[Any]] = {}
    for item in raw:
        if not isinstance(item, dict):
            continue
        definition = item.get("fieldDefinition")
        field_id = definition.get("id") if isinstance(definition, dict) else definition
        if not isinstance(field_id, str) or not field_id or field_id in values:
            continue
        value = _unwrap_field(item.get("value"))
        candidates = [value]
        linked = item.get("linkedEntityId")
        if value is None and isinstance(linked, str) and linked and linked != owner:
            candidates.append(linked)
        values[field_id] = candidates
    return values


def _number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int | float):
        return float(value)
    if isinstance(value, str) and _NUMBER.match(value):
        return float(value)
    return None


def same_value(table: Any, record: Any) -> bool:
    """True when a table cell and a record value show the same thing.

    Empty values (null, ``""``, ``[]``) are equal. Numbers compare by value, so
    a decimal cell ``"1.5"`` equals ``1.5``. Lists compare item by item, or as
    sets of strings.
    """
    if isinstance(table, bool) or isinstance(record, bool):
        return type(table) is type(record) and table == record
    if table == record:
        return True
    if table in _EMPTY and record in _EMPTY:
        return True
    left, right = _number(table), _number(record)
    if left is not None and right is not None:
        return left == right
    if isinstance(table, list) and isinstance(record, list):
        if len(table) != len(record):
            return False
        if all(same_value(a, b) for a, b in zip(table, record, strict=True)):
            return True
        if all(isinstance(item, str) for item in [*table, *record]):
            return sorted(table) == sorted(record)
    return False


def stale_cells(
    row: dict[str, Any],
    fields: dict[int, tuple[str, dict[str, Any]]],
    record: dict[str, Any],
) -> list[dict[str, Any]] | None:
    """Cells of ``row`` that disagree with ``record``, or ``None`` when none compare.

    A column whose field the record does not return is not compared.
    """
    values = _record_values(record)
    if values is None:
        return None
    cells = row.get("cells")
    if not isinstance(cells, list):
        return None
    compared = False
    stale: list[dict[str, Any]] = []
    for index, (field_id, column) in fields.items():
        if index >= len(cells) or field_id not in values or not isinstance(cells[index], dict):
            continue
        compared = True
        shown = _cell_value(cells[index])
        candidates = values[field_id]
        if any(same_value(shown, candidate) for candidate in candidates):
            continue
        stale.append(
            {
                "column_index": index,
                "column_id": column.get("columnId"),
                "column_name": column.get("name"),
                "field_definition_id": field_id,
                "table_value": shown,
                "result_value": candidates[0],
            }
        )
    return stale if compared else None


async def annotate_rows(service: BenchlingService, page: dict[str, Any]) -> dict[str, Any]:
    """Compare each linked row on a ``results rows`` page with its result record.

    A stale row gets ``warning`` and ``stale_cells``. The page gets
    ``stale_check`` (rows compared and rows stale, plus result ids that could not
    be read) and, when a row is stale, ``warnings``. Each linked result is read
    once with ``GET /result/{id}``; nothing is read when no column maps to a field.
    """
    fields = column_fields(page.get("column_definitions"))
    compared = 0
    stale_rows = 0
    unreadable: list[str] = []
    records: dict[str, dict[str, Any] | None] = {}
    raw_items = page.get("items")
    items: list[Any] = raw_items if fields and isinstance(raw_items, list) else []
    for row in items:
        result_id = row_result_id(row)
        if result_id is None or not row_is_comparable(row):
            continue
        if result_id not in records:
            try:
                records[result_id] = await service.get_result(result_id)
            except (NotFoundError, AccessError):
                records[result_id] = None
                unreadable.append(result_id)
        record = records[result_id]
        if not isinstance(record, dict):
            continue
        cells = stale_cells(row, fields, record)
        if cells is None:
            continue
        compared += 1
        if cells:
            stale_rows += 1
            row["warning"] = STALE_CELL_WARNING
            row["stale_cells"] = cells
    check: dict[str, Any] = {"compared_rows": compared, "stale_rows": stale_rows}
    if unreadable:
        check["unreadable_results"] = unreadable
    page["stale_check"] = check
    if stale_rows:
        page["warnings"] = [STALE_CELL_WARNING]
    return page


def _record_entry_id(record: dict[str, Any]) -> str | None:
    entry = record.get("entry")
    if isinstance(entry, dict) and isinstance(entry.get("id"), str) and entry["id"]:
        return str(entry["id"])
    entry_id = record.get("entryId")
    return entry_id if isinstance(entry_id, str) and entry_id else None


def _record_schema_id(record: dict[str, Any]) -> str | None:
    schema = record.get("schema")
    if isinstance(schema, dict) and isinstance(schema.get("id"), str) and schema["id"]:
        return str(schema["id"])
    schema_id = record.get("schemaId")
    return schema_id if isinstance(schema_id, str) and schema_id else None


async def check_result_tables(service: BenchlingService, record: dict[str, Any]) -> dict[str, Any]:
    """Add ``table_check`` to a ``results get`` record, and ``warnings`` when it is stale.

    The V3 result names its entry but not its table, so the entry's results
    tables (matching the record's schema) are read until a row links this
    result. ``table_check.status`` is ``in_sync``, ``stale``, ``not_compared``
    (the row has unsubmitted changes, or no column maps to a returned field),
    ``no_table_row``, ``not_on_entry``, or ``unavailable`` (the entry or a table
    could not be read).
    """
    result_id = record.get("id")
    entry_id = _record_entry_id(record)
    if not isinstance(result_id, str) or not result_id or entry_id is None:
        record["table_check"] = {"status": "not_on_entry"}
        return record
    schema_id = _record_schema_id(record)
    check: dict[str, Any] = {"entry_id": entry_id}
    try:
        parts = [
            part
            async for part in service.api.paginate(
                f"/entry/{entry_id}/parts/items", params={"pageSize": 100}
            )
        ]
        found = None
        for part in parts:
            part_id = part.get("id")
            if not is_results_table(part) or not isinstance(part_id, str) or not part_id:
                continue
            if schema_id is not None and part_result_schema_id(part) not in (None, schema_id):
                continue
            table = await service.get_results_table_part(part_id)
            content = table["content"]
            row = next((item for item in content["rows"] if row_result_id(item) == result_id), None)
            if row is not None:
                found = (part_id, content.get("columnDefinitions"), row)
                break
    except (NotFoundError, AccessError) as exc:
        record["table_check"] = {**check, "status": "unavailable", "reason": exc.code}
        return record
    if found is None:
        record["table_check"] = {**check, "status": "no_table_row"}
        return record
    part_id, columns, row = found
    check.update(part_id=part_id, row_id=row.get("rowId"), row_index=row.get("rowIndex"))
    if not row_is_comparable(row):
        check.update(status="not_compared", submission_status=row.get("submissionStatus"))
        record["table_check"] = check
        return record
    cells = stale_cells(row, column_fields(columns), record)
    if cells is None:
        check["status"] = "not_compared"
    elif cells:
        check.update(status="stale", stale_cells=cells, warning=STALE_CELL_WARNING)
        record["warnings"] = [STALE_CELL_WARNING]
    else:
        check["status"] = "in_sync"
    record["table_check"] = check
    return record
