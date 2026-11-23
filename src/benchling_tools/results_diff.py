"""Compare the results-table rows of two entries, or two runs, table by table.

Rows come from V3 ``GET /document-part-results-table/{id}/rows/items``, read
through the same loader ``export`` uses. Benchling has no server-side filter on a
text value, so a run value selects rows client-side with ``row_filter.row_matches``,
the same match ``results tables --where`` and ``results rows --where`` use; archived
rows and the empty placeholder row are left out on both sides. Field tokens use
the planner's field resolver, and pairs are compared with ``readback`` helpers.
Container barcodes come from V2 ``GET /containers:bulk-get``. Benchling is only read.

With ``pending`` (``--include-pending``), each side also gets the still-pending
results of this home's applied upload plans for its entry, as rows of the table
they were uploaded to: Benchling lists a pending result in no table and on no
entry until a person accepts it, so plans and journals are the only way to place
it. A V2 ``strtbl_…`` table is matched to its part with
``BenchlingService.document_part_for_listed_table``, and each field value goes to
the column whose field-definition id has that system name.

With ``pending_created_after`` (``--pending-created-after``), pending results of
each results table's result schema created at or after that time are added too
(``source`` ``schema_window``). Benchling ties them to no entry or table, so one
goes to the only table on that side with its schema; when several tables share
the schema it is listed in ``pending_unplaced`` instead.
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from . import export, row_filter
from .blob_download import download_blob
from .errors import AccessError, NotFoundError, ValidationError
from .plans.fields import _resolve_field
from .plans.readback import SCHEMA_WINDOW_NOTE, field_mismatches
from .services import (
    BenchlingService,
    is_attachment_field,
    is_results_table,
    part_display_name,
    part_result_schema_id,
)

POSITION_WARNING = (
    "No key was given, so rows are paired by position. A reordered, inserted, or "
    "removed row shows up as changed rows. Pair rows by a field with key."
)
FILE_COLUMNS = [
    "table",
    "change",
    "key",
    "left_row",
    "right_row",
    "field",
    "left",
    "right",
    "left_barcode",
    "right_barcode",
]
PENDING_FILE_COLUMNS = ["left_pending", "right_pending"]
PENDING_WARNING = (
    "Pending results come from this home's applied result upload plans, because Benchling "
    "has no filter for pending results by entry. Plans applied from another home are not "
    "known. Pending rows are numbered after the table's rows and marked pending"
)
PENDING_UNPLACED_WARNING = (
    "Some schema_window pending results were not compared: more than one results table "
    "on that side has their result schema, and Benchling does not say which table a "
    "pending result belongs to. They are listed in pending_unplaced"
)
_SAMPLE_MAX = 200

PendingReader = Callable[..., Awaitable[dict[str, Any]]]


def parse_side(value: object, label: str) -> tuple[str, str | None]:
    """Split ``ENTRY[:RUN_VALUE]``. A URL's ``https://`` is not a separator."""
    if not isinstance(value, str) or not value.strip():
        raise ValidationError(f"{label} must be an entry reference")
    text = value.strip()
    start = len("https://") if text.startswith("https://") else 0
    index = text.find(":", start)
    if index == -1:
        return text, None
    entry, run_value = text[:index].strip(), text[index + 1 :].strip()
    if not entry or not run_value:
        raise ValidationError(
            f"{label} must be ENTRY or ENTRY:RUN_VALUE with both parts non-empty",
            details={"side": label},
        )
    return entry, run_value


async def diff_results(
    service: BenchlingService,
    *,
    left: object,
    right: object,
    run_field: object = None,
    tables: list[str] | None = None,
    key: object = None,
    fields: list[str] | None = None,
    resolve_barcodes: bool = False,
    compare_blobs: bool = False,
    sample: int = 20,
    output: object = None,
    file_format: object = None,
    overwrite: bool = False,
    pending: PendingReader | None = None,
    pending_created_after: str | None = None,
) -> dict[str, Any]:
    """Diff the results tables of two entries, or two runs, and return a summary.

    ``pending`` reads an entry's still-pending results (``entry_pending_results``);
    when given, they are compared as rows of the table they were uploaded to.
    ``pending_created_after`` also reads pending results of each table's schema.
    """
    left_ref, left_run = parse_side(left, "left")
    right_ref, right_run = parse_side(right, "right")
    run_token = _optional_text(run_field, "run_field")
    if (left_run is not None or right_run is not None) and run_token is None:
        raise ValidationError("run_field is required when left or right names a run value")
    key_tokens = _key_tokens(key)
    field_tokens = [_optional_text(item, "fields") or "" for item in fields or []]
    wanted = {_optional_text(item, "table") or "" for item in tables} if tables else None
    if isinstance(sample, bool) or not isinstance(sample, int) or not 1 <= sample <= _SAMPLE_MAX:
        raise ValidationError(f"sample must be an integer from 1 to {_SAMPLE_MAX}")
    window = _optional_text(pending_created_after, "pending_created_after")
    if window is not None and pending is None:
        raise ValidationError("pending_created_after applies only with include_pending")
    destination = None
    selected_format = None
    if output is not None:
        if file_format is None:
            raise ValidationError("format is required with output")
        selected_format = export._format(file_format)
        destination = export._destination(output, overwrite=export._overwrite(overwrite))
    elif file_format is not None:
        raise ValidationError("format applies only with output")

    left_entry = await service.resolve_entry(left_ref)
    right_entry = await service.resolve_entry(right_ref)
    loaded: dict[str, tuple[dict[str, dict[str, Any]], list[str], dict[str, Any] | None]] = {}
    for entry in (left_entry, right_entry):
        entry_id = str(entry["entry_id"])
        if entry_id not in loaded:
            loaded[entry_id] = await _entry_tables(service, entry_id, wanted, pending, window)
    left_tables, left_dupes, left_pending = loaded[str(left_entry["entry_id"])]
    right_tables, right_dupes, right_pending = loaded[str(right_entry["entry_id"])]

    summary: dict[str, Any] = {
        "left": _side_summary(left_entry, left_run),
        "right": _side_summary(right_entry, right_run),
        "run_field": run_token,
        "key": key_tokens or None,
        "pairing": "key" if key_tokens else "position",
        "tables_only_left": [
            name for name in left_tables if name not in right_tables and name not in right_dupes
        ],
        "tables_only_right": [
            name for name in right_tables if name not in left_tables and name not in left_dupes
        ],
        "schema_mismatch": [],
        "tables": [],
    }
    unplaced: dict[str, list[dict[str, Any]]] = {}
    if pending is not None:
        assert left_pending is not None and right_pending is not None
        unplaced = {"left": left_pending.pop("unplaced"), "right": right_pending.pop("unplaced")}
        summary["pending"] = {"left": left_pending, "right": right_pending}
        summary["pending_unplaced"] = unplaced
    if left_dupes or right_dupes:
        summary["duplicate_table_names"] = {"left": left_dupes, "right": right_dupes}
    if wanted:
        missing = sorted(name for name in wanted if name not in left_tables | right_tables)
        if missing:
            summary["tables_not_found"] = missing

    schema_fields: dict[str, dict[str, dict[str, Any]]] = {}
    plans: list[dict[str, Any]] = []
    for name, left_table in left_tables.items():
        right_table = right_tables.get(name)
        if right_table is None:
            continue
        if left_table["schema_id"] != right_table["schema_id"]:
            summary["schema_mismatch"].append(
                {
                    "table": name,
                    "left_part_id": left_table["part_id"],
                    "right_part_id": right_table["part_id"],
                    "left_schema_id": left_table["schema_id"],
                    "right_schema_id": right_table["schema_id"],
                }
            )
            continue
        definitions = await _schema_fields(service, left_table["schema_id"], schema_fields)
        plans.append(
            _plan_table(
                left_table,
                right_table,
                definitions,
                left_run=left_run,
                right_run=right_run,
                run_token=run_token,
                key_tokens=key_tokens,
                field_tokens=field_tokens,
            )
        )

    barcodes: dict[str, str] | None = None
    if resolve_barcodes:
        ids = sorted(
            {
                container
                for plan in plans
                for record in [*plan["left"], *plan["right"]]
                for container in record["containers"].values()
            }
        )
        found = await service.bulk_get_containers(ids) if ids else {}
        barcodes = {
            container_id: item["barcode"]
            for container_id, item in found.items()
            if isinstance(item.get("barcode"), str) and item["barcode"]
        }
        summary["containers"] = {
            "requested": len(ids),
            "resolved": len(barcodes),
            "unresolved": [item for item in ids if item not in barcodes][:sample],
        }

    file_rows: list[dict[str, Any]] = []
    if compare_blobs:
        with tempfile.TemporaryDirectory(prefix="benchling-tools-diff-") as scratch:
            blobs = _BlobHasher(service, Path(scratch))
            for plan in plans:
                summary["tables"].append(
                    await _diff_table(plan, barcodes, blobs, sample, file_rows)
                )
    else:
        for plan in plans:
            summary["tables"].append(await _diff_table(plan, barcodes, None, sample, file_rows))

    warnings: list[str] = []
    if not key_tokens and plans:
        warnings.append(POSITION_WARNING)
    if pending is not None:
        warnings.append(PENDING_WARNING)
    if window is not None:
        warnings.append(SCHEMA_WINDOW_NOTE)
    if unplaced.get("left") or unplaced.get("right"):
        warnings.append(PENDING_UNPLACED_WARNING)
    if warnings:
        summary["warnings"] = warnings
    if destination is not None and selected_format is not None:
        source = {"left": summary["left"], "right": summary["right"]}
        columns = [*FILE_COLUMNS, *PENDING_FILE_COLUMNS] if pending is not None else FILE_COLUMNS
        payload = export._render(
            "results-diff", columns, file_rows, source, file_format=selected_format
        )
        export._write(destination, payload, overwrite=bool(overwrite))
        summary["output"] = {
            "path": str(destination),
            "format": selected_format,
            "row_count": len(file_rows),
            "checksum": hashlib.sha256(payload).hexdigest(),
            "checksum_algorithm": "sha256",
        }
    return summary


def _optional_text(value: object, label: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise ValidationError(f"{label} must be a non-empty string")
    return value.strip()


def _key_tokens(value: object) -> list[str]:
    text = _optional_text(value, "key")
    if text is None:
        return []
    tokens = [item.strip() for item in text.split(",")]
    if any(not item for item in tokens):
        raise ValidationError("key must be FIELD or FIELD,FIELD with no empty field")
    return tokens


def _side_summary(entry: dict[str, Any], run_value: str | None) -> dict[str, Any]:
    side = {key: entry.get(key) for key in ("entry_id", "display_id", "name", "web_url")}
    if run_value is not None:
        side["run_value"] = run_value
    return side


async def _entry_tables(
    service: BenchlingService,
    entry_id: str,
    wanted: set[str] | None,
    pending: PendingReader | None = None,
    created_after: str | None = None,
) -> tuple[dict[str, dict[str, Any]], list[str], dict[str, Any] | None]:
    """Every results table on the entry, by exact name, the names used twice, and,
    with ``pending``, a summary of the pending results attached to those tables."""
    parts = [
        item
        async for item in service.api.paginate(
            f"/entry/{entry_id}/parts/items", params={"pageSize": 100}
        )
    ]
    by_name: dict[str, list[dict[str, Any]]] = {}
    for part in parts:
        if not is_results_table(part) or not isinstance(part.get("id"), str):
            continue
        listed = part_display_name(part)
        if wanted is not None and listed is not None and listed not in wanted:
            continue
        table = await _load_table(service, str(part["id"]), listed)
        if wanted is not None and table["name"] not in wanted:
            continue
        by_name.setdefault(table["name"], []).append(table)
    duplicates = [name for name, found in by_name.items() if len(found) > 1]
    tables = {name: found[0] for name, found in by_name.items() if len(found) == 1}
    if pending is None:
        return tables, duplicates, None
    return (
        tables,
        duplicates,
        await _attach_pending(service, entry_id, tables, parts, pending, created_after),
    )


async def _attach_pending(
    service: BenchlingService,
    entry_id: str,
    tables: dict[str, dict[str, Any]],
    parts: list[dict[str, Any]],
    pending: PendingReader,
    created_after: str | None = None,
) -> dict[str, Any]:
    """Give each loaded table the pending results uploaded to it, in ``pending``.

    A this_home record goes to the part its ``strtbl_…`` table matches. A
    schema_window record goes to the only results-table part on the entry with
    its schema; with two or more it is returned in ``unplaced``. A record whose
    table matches no single part, or a part not compared here (another name, or
    filtered out by ``tables``), is not attached and is counted in ``not_attached``.
    """
    results_parts = [
        part for part in parts if is_results_table(part) and isinstance(part.get("id"), str)
    ]
    parts_by_schema: dict[str, list[dict[str, Any]]] = {}
    for part in results_parts:
        schema_id = part_result_schema_id(part)
        if schema_id:
            parts_by_schema.setdefault(schema_id, []).append(part)
    found = await pending(entry_id, created_after=created_after, schema_ids=sorted(parts_by_schema))
    items = [item for item in found.get("items") or [] if isinstance(item, dict)]
    by_part = {str(table["part_id"]): table for table in tables.values()}
    for table in tables.values():
        table["pending"] = []
    part_of: dict[str, str | None] = {}
    unplaced: list[dict[str, Any]] = []
    for item in items:
        if item.get("source") != "schema_window":
            continue
        candidates = parts_by_schema.get(str(item.get("schema_id")), [])
        if len(candidates) > 1:
            unplaced.append(
                {
                    "id": item.get("id"),
                    "v3_id": item.get("v3_id"),
                    "schema_id": item.get("schema_id"),
                    "created_at": item.get("created_at"),
                    "tables": [part_display_name(part) or part["id"] for part in candidates],
                }
            )
        item["_part_id"] = str(candidates[0]["id"]) if len(candidates) == 1 else None
    table_ids = {str(item["table_id"]) for item in items if isinstance(item.get("table_id"), str)}
    if table_ids:
        listed = await service.list_result_tables(entry_id)
        notes = {str(note.get("table_id")): note for note in listed["items"]}
        for table_id in sorted(table_ids):
            note = notes.get(table_id)
            part_of[table_id] = (
                await service.document_part_for_listed_table(
                    entry_id, note, unstructured=False, parts=parts
                )
                if note is not None
                else None
            )
    attached = 0
    for item in items:
        if "_part_id" in item:
            part_id = item.pop("_part_id")
        else:
            part_id = part_of.get(str(item.get("table_id")))
        target = by_part.get(part_id) if part_id is not None else None
        if target is None:
            continue
        target["pending"].append(item)
        attached += 1
    return {
        "plans": len(found.get("plans") or []),
        "pending": len(items),
        "attached": attached,
        "not_attached": len(items) - attached - len(unplaced),
        "unplaced_count": len(unplaced),
        "not_found": len(found.get("not_found") or []),
        "sources": found.get("sources"),
        "unplaced": unplaced,
    }


async def _load_table(
    service: BenchlingService, part_id: str, listed: str | None
) -> dict[str, Any]:
    first, raw_rows = await export._read_results_rows(service, part_id)
    definitions = [item for item in first.get("column_definitions") or [] if isinstance(item, dict)]
    columns = export._column_names(definitions, export._width(raw_rows))
    name = first.get("name") if isinstance(first.get("name"), str) else None
    return {
        "part_id": part_id,
        "name": name or listed or part_id,
        "schema_id": first.get("result_schema_id"),
        "definitions": definitions,
        "columns": columns,
        "raw_rows": raw_rows,
        "rows": [export._table_row(row, columns) for row in raw_rows],
    }


async def _schema_fields(
    service: BenchlingService,
    schema_id: str | None,
    cache: dict[str, dict[str, dict[str, Any]]],
) -> dict[str, dict[str, Any]]:
    """Result-schema field definitions by id, for system names and attachment fields."""
    if not schema_id:
        return {}
    if schema_id not in cache:
        try:
            detail = await service.get_schema(schema_id, kind="result")
        except (NotFoundError, AccessError):
            detail = {}
        cache[schema_id] = {
            str(item["id"]): item
            for item in detail.get("field_definitions") or []
            if isinstance(item, dict) and isinstance(item.get("id"), str)
        }
    return cache[schema_id]


def _column_definitions(
    table: dict[str, Any], schema_fields: dict[str, dict[str, Any]]
) -> list[dict[str, Any]]:
    """One resolver definition per column: field id, system name, and display name."""
    resolved: list[dict[str, Any]] = []
    for index, column in enumerate(table["columns"]):
        raw = table["definitions"][index] if index < len(table["definitions"]) else {}
        field_id = raw.get("fieldDefinitionApiId")
        schema_field = schema_fields.get(field_id) if isinstance(field_id, str) else None
        system_name = (schema_field or {}).get("systemName") or raw.get("systemName")
        resolved.append(
            {
                "id": field_id or raw.get("columnId") or column,
                "systemName": system_name,
                "name": raw.get("name") or column,
                "column": column,
                "columnId": raw.get("columnId"),
                "attachment": bool(schema_field and is_attachment_field(schema_field)),
            }
        )
    return resolved


def _column_for(token: str, definitions: list[dict[str, Any]], table: str, side: str) -> str:
    found = _resolve_field(token, definitions)
    if found is None:
        found = next(
            (item for item in definitions if token in (item["columnId"], item["column"])), None
        )
    if isinstance(found, list):
        raise ValidationError(
            "Field matches more than one column",
            details={"field": token, "table": table, "columns": [d["column"] for d in found]},
        )
    if found is None:
        raise ValidationError(
            "Field is not a column of this results table",
            details={
                "field": token,
                "table": table,
                "side": side,
                "columns": [item["column"] for item in definitions],
            },
        )
    return str(found["column"])


def _plan_table(
    left: dict[str, Any],
    right: dict[str, Any],
    schema_fields: dict[str, dict[str, Any]],
    *,
    left_run: str | None,
    right_run: str | None,
    run_token: str | None,
    key_tokens: list[str],
    field_tokens: list[str],
) -> dict[str, Any]:
    """Resolve the run, key, and compared columns and select each side's rows."""
    sides: dict[str, Any] = {}
    for label, table, run_value in (("left", left, left_run), ("right", right, right_run)):
        definitions = _column_definitions(table, schema_fields)
        run_column = (
            _column_for(run_token, definitions, table["name"], label) if run_token else None
        )
        sides[label] = {
            "definitions": definitions,
            "run_column": run_column,
            "keys": [_column_for(t, definitions, table["name"], label) for t in key_tokens],
            "fields": [_column_for(t, definitions, table["name"], label) for t in field_tokens],
            "records": _records(table, definitions, run_column, run_value),
        }
    if field_tokens:
        compared = list(dict.fromkeys(sides["left"]["fields"]))
    else:
        excluded = {sides["left"]["run_column"], sides["right"]["run_column"]}
        compared = [
            column
            for column in dict.fromkeys([*left["columns"], *right["columns"]])
            if column not in excluded
        ]
    blob_fields = {
        item["column"]
        for side in sides.values()
        for item in side["definitions"]
        if item["attachment"]
    }
    return {
        "table": left["name"],
        "schema_id": left["schema_id"],
        "left_part_id": left["part_id"],
        "right_part_id": right["part_id"],
        "fields": compared,
        "left_keys": sides["left"]["keys"],
        "right_keys": sides["right"]["keys"],
        "blob_fields": blob_fields,
        "left": sides["left"]["records"],
        "right": sides["right"]["records"],
        "include_pending": "pending" in left or "pending" in right,
    }


def _records(
    table: dict[str, Any],
    definitions: list[dict[str, Any]],
    run_column: str | None,
    run_value: str | None,
) -> list[dict[str, Any]]:
    rows = table["rows"]
    bound: list[tuple[int, str]] = []
    if run_value is not None and run_column is not None:
        bound = [(table["columns"].index(run_column), run_value)]
    rows = list(rows)
    raw_rows = list(table["raw_rows"])
    pending_ids: list[Any] = [None] * len(raw_rows)
    sources: list[Any] = [None] * len(raw_rows)
    for item in table.get("pending") or []:
        raw = _pending_row(item, definitions)
        raw_rows.append(raw)
        rows.append(export._table_row(raw, table["columns"]))
        pending_ids.append(item.get("v3_id") or item.get("id"))
        sources.append(item.get("source"))
    kept = {id(row) for row, raw in zip(rows, raw_rows) if row_filter.row_matches(raw, bound)}
    attachment_columns = {item["column"] for item in definitions if item["attachment"]}
    records = []
    for index, (row, raw) in enumerate(zip(rows, raw_rows)):
        if id(row) not in kept:
            continue
        containers: dict[str, str] = {}
        attachments: dict[str, str] = {}
        for column, cell in zip(table["columns"], raw["cells"]):
            typename, linked = _link(cell)
            if linked is None:
                continue
            if typename == "Container" or (typename is None and linked.startswith("con_")):
                containers[column] = linked
            elif typename == "Attachment" or column in attachment_columns:
                attachments[column] = linked
        record = {
            "index": index,
            "values": row,
            "containers": containers,
            "attachments": attachments,
        }
        if pending_ids[index] is not None:
            record.update(pending=True, result_id=pending_ids[index], source=sources[index])
        records.append(record)
    return records


def _pending_row(item: dict[str, Any], definitions: list[dict[str, Any]]) -> dict[str, Any]:
    """A pending result as a table row: each field value in the column with its system name."""
    fields = item.get("fields")
    if not isinstance(fields, dict):
        fields = {}
    cells = []
    for definition in definitions:
        name = definition.get("systemName")
        value = fields.get(name) if isinstance(name, str) else None
        cells.append({"inputValue": {"value": value}})
    return {"cells": cells, "submissionStatus": "PENDING"}


def _link(cell: dict[str, Any]) -> tuple[str | None, str | None]:
    """The linked object's typename and id in a cell's entered value."""
    entered = cell.get("inputValue")
    value = entered.get("value") if isinstance(entered, dict) else None
    if isinstance(value, dict):
        typename = value.get("__typename")
        linked = value.get("id")
        if isinstance(linked, str) and linked:
            return (typename if isinstance(typename, str) else None), linked
    if isinstance(value, str) and value:
        return None, value
    return None, None


class _BlobHasher:
    """SHA-256 per blob id, downloaded once into a scratch directory and then deleted."""

    def __init__(self, service: BenchlingService, scratch: Path) -> None:
        self.service = service
        self.scratch = scratch
        self.digests: dict[str, str] = {}

    async def sha256(self, blob_id: str) -> str:
        if blob_id not in self.digests:
            path = self.scratch / f"blob-{len(self.digests)}"
            try:
                downloaded = await download_blob(self.service, blob_id, str(path), force=True)
            finally:
                if path.exists():
                    os.unlink(path)
            self.digests[blob_id] = str(downloaded["sha256"])
        return self.digests[blob_id]


def _key(
    record: dict[str, Any], columns: list[str], barcodes: dict[str, str] | None
) -> tuple[str, Any]:
    """A comparable key and its display value. A container keys on its barcode when known."""
    values = []
    for column in columns:
        value = record["values"].get(column)
        container = record["containers"].get(column)
        if barcodes is not None and container is not None:
            value = barcodes.get(container, value)
        values.append(value)
    shown = values[0] if len(values) == 1 else values
    return json.dumps(values, sort_keys=True, default=str), shown


def _pairs(
    plan: dict[str, Any], barcodes: dict[str, str] | None
) -> tuple[
    list[tuple[Any, dict[str, Any], dict[str, Any]]], list[Any], list[Any], list[dict[str, Any]]
]:
    """Matched pairs, rows only on each side, and duplicated keys.

    A key used more than once on either side is not paired. Its entry in the
    duplicates list carries the row count and row indexes on both sides, so a
    key that is unique on one side and repeated on the other stays visible.
    """
    left, right = plan["left"], plan["right"]
    if not plan["left_keys"]:
        pairs = [(index, a, b) for index, (a, b) in enumerate(zip(left, right))]
        only_left = [(record["index"], record) for record in left[len(right) :]]
        only_right = [(record["index"], record) for record in right[len(left) :]]
        return pairs, only_left, only_right, []
    groups: dict[str, dict[str, list[dict[str, Any]]]] = {"left": {}, "right": {}}
    shown: dict[str, Any] = {}
    for label, records, columns in (
        ("left", left, plan["left_keys"]),
        ("right", right, plan["right_keys"]),
    ):
        for record in records:
            hashed, display = _key(record, columns, barcodes)
            shown.setdefault(hashed, display)
            groups[label].setdefault(hashed, []).append(record)
    duplicates = [
        {
            "key": shown[hashed],
            "left": len(groups["left"].get(hashed, [])),
            "right": len(groups["right"].get(hashed, [])),
            "left_row_indexes": [record["index"] for record in groups["left"].get(hashed, [])],
            "right_row_indexes": [record["index"] for record in groups["right"].get(hashed, [])],
        }
        for hashed in shown
        if len(groups["left"].get(hashed, [])) > 1 or len(groups["right"].get(hashed, [])) > 1
    ]
    unique = {
        label: {hashed: found[0] for hashed, found in groups[label].items() if len(found) == 1}
        for label in ("left", "right")
    }
    pairs = [
        (shown[hashed], record, unique["right"][hashed])
        for hashed, record in unique["left"].items()
        if hashed in unique["right"]
    ]
    only_left = [
        (shown[hashed], record)
        for hashed, record in unique["left"].items()
        if hashed not in groups["right"]
    ]
    only_right = [
        (shown[hashed], record)
        for hashed, record in unique["right"].items()
        if hashed not in groups["left"]
    ]
    return pairs, only_left, only_right, duplicates


async def _changes(
    left: dict[str, Any],
    right: dict[str, Any],
    plan: dict[str, Any],
    barcodes: dict[str, str] | None,
    blobs: _BlobHasher | None,
) -> list[dict[str, Any]]:
    """Fields that differ in one pair, compared with ``readback.field_mismatches``."""
    fields = plan["fields"]
    record = {
        "fields": {
            field: {"value": right["values"][field]} for field in fields if field in right["values"]
        }
    }
    changes = []
    for problem in field_mismatches(
        {field: left["values"].get(field) for field in fields}, record, None
    ):
        field = problem["field"]
        change: dict[str, Any] = {"field": field, "left": problem["planned"]}
        change["right"] = problem["actual"]
        left_blob, right_blob = left["attachments"].get(field), right["attachments"].get(field)
        if blobs is not None and left_blob and right_blob:
            left_sha, right_sha = await blobs.sha256(left_blob), await blobs.sha256(right_blob)
            plan["blob_counts"]["compared"] += 1
            if left_sha == right_sha:
                continue
            plan["blob_counts"]["changed"] += 1
            change.update(blob_changed=True, left_sha256=left_sha, right_sha256=right_sha)
        if barcodes is not None:
            for label, side in (("left", left), ("right", right)):
                container = side["containers"].get(field)
                if container is not None:
                    change[f"{label}_barcode"] = barcodes.get(container)
        changes.append(change)
    return changes


def _row_view(key: Any, record: dict[str, Any], barcodes: dict[str, str] | None) -> dict[str, Any]:
    view: dict[str, Any] = {"key": key, "row_index": record["index"], "values": record["values"]}
    if record.get("pending"):
        view.update(pending=True, result_id=record["result_id"], source=record["source"])
    if barcodes is not None and record["containers"]:
        view["barcodes"] = {
            column: barcodes.get(container) for column, container in record["containers"].items()
        }
    return view


async def _diff_table(
    plan: dict[str, Any],
    barcodes: dict[str, str] | None,
    blobs: _BlobHasher | None,
    sample: int,
    file_rows: list[dict[str, Any]],
) -> dict[str, Any]:
    plan["blob_counts"] = {"compared": 0, "changed": 0}
    pending = bool(plan["include_pending"])
    pairs, only_left, only_right, duplicates = _pairs(plan, barcodes)
    changed: list[dict[str, Any]] = []
    changed_rows = 0
    for key, left, right in pairs:
        changes = await _changes(left, right, plan, barcodes, blobs)
        if changes:
            changed_rows += 1
        for change in changes:
            for label, record in (("left", left), ("right", right)):
                if record.get("pending"):
                    change[f"{label}_pending"] = True
                    change[f"{label}_source"] = record["source"]
            entry = {"key": key, **change}
            changed.append(entry)
            file_rows.append(
                _file_row(
                    plan["table"], "changed", key, left["index"], right["index"], change, pending
                )
            )
    for label, rows in (("only_left", only_left), ("only_right", only_right)):
        for key, record in rows:
            left_index = record["index"] if label == "only_left" else None
            right_index = record["index"] if label == "only_right" else None
            side = "left" if label == "only_left" else "right"
            values = {side: record["values"]}
            if record.get("pending"):
                values[f"{side}_pending"] = True
            file_rows.append(
                _file_row(plan["table"], label, key, left_index, right_index, values, pending)
            )
    table: dict[str, Any] = {
        "table": plan["table"],
        "schema_id": plan["schema_id"],
        "left_part_id": plan["left_part_id"],
        "right_part_id": plan["right_part_id"],
        "fields": plan["fields"],
        "counts": {
            "left": len(plan["left"]),
            "right": len(plan["right"]),
            "matched": len(pairs),
            "only_left": len(only_left),
            "only_right": len(only_right),
            "changed": changed_rows,
            "duplicate_keys": len(duplicates),
        },
        "only_left": [_row_view(key, record, barcodes) for key, record in only_left[:sample]],
        "only_right": [_row_view(key, record, barcodes) for key, record in only_right[:sample]],
        "changed": changed[:sample],
        "duplicate_keys": duplicates,
    }
    if blobs is not None:
        table["blobs"] = plan["blob_counts"]
    if pending:
        for label in ("left", "right"):
            table["counts"][f"pending_{label}"] = sum(
                bool(record.get("pending")) for record in plan[label]
            )
    return table


def _file_row(
    table: str,
    change: str,
    key: Any,
    left_row: int | None,
    right_row: int | None,
    values: dict[str, Any],
    pending: bool = False,
) -> dict[str, Any]:
    row = {
        "table": table,
        "change": change,
        "key": key,
        "left_row": left_row,
        "right_row": right_row,
        "field": values.get("field"),
        "left": values.get("left"),
        "right": values.get("right"),
        "left_barcode": values.get("left_barcode"),
        "right_barcode": values.get("right_barcode"),
    }
    if pending:
        row["left_pending"] = bool(values.get("left_pending"))
        row["right_pending"] = bool(values.get("right_pending"))
    return row
