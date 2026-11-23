"""Client-side ``--where FIELD=VALUE`` filters over results-table rows.

Benchling has no server-side filter on text field values. V3 ``GET /result/items``
filters by entry, schema, creator, project, time, id and link values only, and
V2 ``GET /assay-results`` by schema, time, entity, storage, run and id. So a
text ``--where`` is evaluated here, over the rows of
``GET /document-part-results-table/{id}/rows/items``. Pending results are not
table rows until a person accepts them, so they are never counted.

FIELD is resolved against the table's ``columnDefinitions`` with the field
resolver the planner uses (``plans.fields._resolve_field``): a display name,
a system name (from the table's result schema), or a field-definition id. A
cell matches when its value, the evaluation when Benchling returned one and the
input otherwise, is exactly VALUE as text. A link cell matches on the linked id.
Archived rows and the empty ``UNSUBMITTED`` placeholder row (``capacity.is_placeholder_row``)
never match.
"""

from __future__ import annotations

import json
import shlex
from typing import Any

from .capacity import is_placeholder_row
from .errors import AccessError, NotFoundError, ValidationError
from .export import _cell_value
from .plans.fields import _resolve_field
from .services import BenchlingService

WHERE_NOTE = (
    "--where is evaluated client-side over accepted table rows, because Benchling has no "
    "server-side filter on text field values. Pending results are not table rows and are "
    "not counted; use results pending for them."
)


def parse_where(where: object) -> list[tuple[str, str]]:
    """``["FIELD=VALUE", …]`` to ``[(FIELD, VALUE), …]``; split at the first ``=``."""
    if where is None:
        return []
    if not isinstance(where, list):
        raise ValidationError("where must be a list of FIELD=VALUE strings")
    conditions: list[tuple[str, str]] = []
    for item in where:
        if not isinstance(item, str) or "=" not in item:
            raise ValidationError(
                "where must be FIELD=VALUE",
                details={"where": item if isinstance(item, str) else None},
            )
        field, value = item.split("=", 1)
        if not field.strip():
            raise ValidationError("where FIELD must not be empty", details={"where": item})
        conditions.append((field.strip(), value))
    return conditions


def describe(conditions: list[tuple[str, str]]) -> dict[str, Any]:
    return {
        "conditions": [{"field": field, "value": value} for field, value in conditions],
        "client_side": True,
        "note": WHERE_NOTE,
    }


async def table_fields(
    service: BenchlingService,
    columns: object,
    schema_id: object,
    cache: dict[str, list[dict[str, Any]]],
) -> list[dict[str, Any]]:
    """One definition per column, shaped for ``_resolve_field``, with its column index.

    A schema-field column takes its system name from the result schema, read
    once per schema with ``get_schema``. Other columns match by name or column id.
    """
    schema_defs: dict[str, dict[str, Any]] = {}
    if isinstance(schema_id, str) and schema_id:
        if schema_id not in cache:
            try:
                loaded = await service.get_schema(schema_id)
            except (NotFoundError, AccessError):
                cache[schema_id] = []
            else:
                raw = loaded.get("field_definitions")
                cache[schema_id] = (
                    [item for item in raw if isinstance(item, dict)]
                    if isinstance(raw, list)
                    else []
                )
        schema_defs = {
            str(item["id"]): item for item in cache[schema_id] if isinstance(item.get("id"), str)
        }
    definitions: list[dict[str, Any]] = []
    for index, column in enumerate(columns if isinstance(columns, list) else []):
        if not isinstance(column, dict):
            continue
        field_id = column.get("fieldDefinitionApiId")
        if isinstance(field_id, str) and field_id:
            schema_field = schema_defs.get(field_id, {})
            definitions.append(
                {
                    "id": field_id,
                    "systemName": schema_field.get("systemName"),
                    "name": column.get("name") or schema_field.get("name"),
                    "archived": bool(column.get("isArchived")),
                    "column_index": index,
                }
            )
        else:
            definitions.append(
                {"id": column.get("columnId"), "name": column.get("name"), "column_index": index}
            )
    return definitions


def available_fields(definitions: list[dict[str, Any]]) -> dict[str, list[str]]:
    def names(key: str) -> list[str]:
        return sorted(
            {str(item[key]) for item in definitions if isinstance(item.get(key), str) and item[key]}
        )

    return {
        "available_display_names": names("name"),
        "available_system_names": names("systemName"),
        "available_field_ids": names("id"),
    }


def bind(
    conditions: list[tuple[str, str]], definitions: list[dict[str, Any]]
) -> tuple[list[tuple[int, str]], list[str]]:
    """Column index and value per condition, plus the fields that match no column."""
    bound: list[tuple[int, str]] = []
    missing: list[str] = []
    for field, value in conditions:
        resolved = _resolve_field(field, definitions)
        if resolved is None:
            missing.append(field)
        elif isinstance(resolved, list):
            raise ValidationError(
                f"where field {field} matches more than one column",
                details={
                    "field": field,
                    "candidates": [
                        {"column_name": item.get("name"), "field_definition_id": item.get("id")}
                        for item in resolved
                    ],
                },
            )
        else:
            bound.append((int(resolved["column_index"]), value))
    return bound, missing


def _texts(value: Any) -> set[str]:
    if isinstance(value, list):
        return {text for item in value for text in _texts(item)}
    if value is None:
        return {""}
    if isinstance(value, bool):
        return {"true" if value else "false"}
    if isinstance(value, str | int | float):
        return {str(value)}
    return {json.dumps(value, sort_keys=True)}


def row_matches(row: dict[str, Any], bound: list[tuple[int, str]]) -> bool:
    if row.get("isArchived") or is_placeholder_row(row):
        return False
    cells = row.get("cells")
    if not isinstance(cells, list):
        return False
    for index, expected in bound:
        if index >= len(cells) or not isinstance(cells[index], dict):
            return False
        if expected not in _texts(_cell_value(cells[index])):
            return False
    return True


async def filter_page(
    service: BenchlingService, page: dict[str, Any], conditions: list[tuple[str, str]]
) -> dict[str, Any]:
    """Keep only the rows of one ``results rows`` page that match every condition.

    Adds ``scanned`` (rows on the page) and ``matched``. ``next_token`` still
    pages the underlying table. The filter covers only this page, so the reply
    says so: ``where_scope`` is ``"page"``, ``complete`` is false while a next
    page exists, and then ``where_scope_note`` points at ``results tables
    --where`` for whole-table counts. An unknown field is a validation error.
    """
    definitions = await table_fields(
        service, page.get("column_definitions"), page.get("result_schema_id"), {}
    )
    bound, missing = bind(conditions, definitions)
    if missing:
        raise ValidationError(
            f"Unknown where field: {', '.join(missing)}",
            details={"unknown_fields": missing, **available_fields(definitions)},
        )
    items = page.get("items") or []
    kept = [row for row in items if isinstance(row, dict) and row_matches(row, bound)]
    page["items"] = kept
    page["scanned"] = len(items)
    page["matched"] = len(kept)
    page["where"] = describe(conditions)
    complete = not page.get("next_token")
    page["where_scope"] = "page"
    page["complete"] = complete
    if not complete:
        flags = " ".join(
            f"--where {shlex.quote(f'{field}={value}')}" for field, value in conditions
        )
        page["where_scope_note"] = (
            f"--where filtered this page only ({len(kept)} of {len(items)} rows read match); "
            "more rows follow (next_token). For whole-table counts run "
            f"`results tables ENTRY {flags}`, or page on with --cursor."
        )
    return page


async def count_tables(
    service: BenchlingService, result: dict[str, Any], conditions: list[tuple[str, str]]
) -> dict[str, Any]:
    """Add ``matching_rows`` to each listed table by reading every row of it.

    A table without one of the fields gets ``matching_rows: null`` and a
    ``where_note``. When no table has a field, that is a validation error.
    """
    cache: dict[str, list[dict[str, Any]]] = {}
    total = 0
    examined = 0
    unknown: dict[str, None] = {}
    seen_definitions: list[dict[str, Any]] = []
    any_bound = False
    for item in result.get("items") or []:
        part_id = item.get("part_id")
        if not isinstance(part_id, str) or not part_id:
            item["matching_rows"] = None
            item["where_note"] = "Table was not counted: no single v3_document_part matched it."
            continue
        table = await service.get_results_table_part(part_id)
        content = table["content"]
        schema = content.get("resultSchema")
        schema_id = schema.get("id") if isinstance(schema, dict) else item.get("schema_id")
        definitions = await table_fields(
            service, content.get("columnDefinitions"), schema_id, cache
        )
        seen_definitions.extend(definitions)
        examined += 1
        bound, missing = bind(conditions, definitions)
        if missing:
            unknown.update(dict.fromkeys(missing))
            item["matching_rows"] = None
            item["where_note"] = f"Table has no column for: {', '.join(missing)}."
            continue
        any_bound = True
        rows = content.get("rows") or []
        item["rows_scanned"] = len(rows)
        item["matching_rows"] = sum(1 for row in rows if row_matches(row, bound))
        total += len(rows)
    if examined and not any_bound:
        raise ValidationError(
            f"Unknown where field: {', '.join(unknown)}",
            details={"unknown_fields": list(unknown), **available_fields(seen_definitions)},
        )
    result["rows_scanned"] = total
    result["where"] = describe(conditions)
    return result
