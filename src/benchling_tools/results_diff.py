"""Compare the results-table rows of two entries, or two runs, table by table.

Rows come from V3 ``GET /document-part-results-table/{id}/rows/items``, read
through the same loader ``export`` uses. Benchling has no server-side filter on a
text value, so a run value selects rows client-side with ``row_filter.row_matches``,
the same match ``results tables --where`` and ``results rows --where`` use; archived
rows and the empty placeholder row are left out on both sides. Field tokens use
the planner's field resolver, and pairs are compared with ``readback`` helpers.
Container barcodes come from V2 ``GET /containers:bulk-get``. Benchling is only read.
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from pathlib import Path
from typing import Any

from . import export, row_filter
from .blob_download import download_blob
from .errors import AccessError, NotFoundError, ValidationError
from .plans.fields import _resolve_field
from .plans.readback import field_mismatches
from .services import (
    BenchlingService,
    is_attachment_field,
    is_results_table,
    part_display_name,
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
_SAMPLE_MAX = 200


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
) -> dict[str, Any]:
    """Diff the results tables of two entries, or two runs, and return a summary."""
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
    loaded: dict[str, tuple[dict[str, dict[str, Any]], list[str]]] = {}
    for entry in (left_entry, right_entry):
        entry_id = str(entry["entry_id"])
        if entry_id not in loaded:
            loaded[entry_id] = await _entry_tables(service, entry_id, wanted)
    left_tables, left_dupes = loaded[str(left_entry["entry_id"])]
    right_tables, right_dupes = loaded[str(right_entry["entry_id"])]

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
    if warnings:
        summary["warnings"] = warnings
    if destination is not None and selected_format is not None:
        source = {"left": summary["left"], "right": summary["right"]}
        payload = export._render(
            "results-diff", FILE_COLUMNS, file_rows, source, file_format=selected_format
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
    service: BenchlingService, entry_id: str, wanted: set[str] | None
) -> tuple[dict[str, dict[str, Any]], list[str]]:
    """Every results table on the entry, by exact name, and the names used twice."""
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
    return {name: found[0] for name, found in by_name.items() if len(found) == 1}, duplicates


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
    kept = {
        id(row) for row, raw in zip(rows, table["raw_rows"]) if row_filter.row_matches(raw, bound)
    }
    attachment_columns = {item["column"] for item in definitions if item["attachment"]}
    records = []
    for index, (row, raw) in enumerate(zip(rows, table["raw_rows"])):
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
        records.append(
            {"index": index, "values": row, "containers": containers, "attachments": attachments}
        )
    return records


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
) -> tuple[list[tuple[Any, dict[str, Any], dict[str, Any]]], list[Any], list[Any], dict[str, Any]]:
    """Matched pairs, rows only on each side, and duplicate keys per side."""
    left, right = plan["left"], plan["right"]
    if not plan["left_keys"]:
        pairs = [(index, a, b) for index, (a, b) in enumerate(zip(left, right))]
        only_left = [(record["index"], record) for record in left[len(right) :]]
        only_right = [(record["index"], record) for record in right[len(left) :]]
        return pairs, only_left, only_right, {"left": [], "right": []}
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
    duplicates = {
        label: [
            {"key": shown[hashed], "row_indexes": [record["index"] for record in found]}
            for hashed, found in groups[label].items()
            if len(found) > 1
        ]
        for label in ("left", "right")
    }
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
    pairs, only_left, only_right, duplicates = _pairs(plan, barcodes)
    changed: list[dict[str, Any]] = []
    changed_rows = 0
    for key, left, right in pairs:
        changes = await _changes(left, right, plan, barcodes, blobs)
        if changes:
            changed_rows += 1
        for change in changes:
            entry = {"key": key, **change}
            changed.append(entry)
            file_rows.append(
                _file_row(plan["table"], "changed", key, left["index"], right["index"], change)
            )
    for label, rows in (("only_left", only_left), ("only_right", only_right)):
        for key, record in rows:
            left_index = record["index"] if label == "only_left" else None
            right_index = record["index"] if label == "only_right" else None
            values = {"left" if label == "only_left" else "right": record["values"]}
            file_rows.append(_file_row(plan["table"], label, key, left_index, right_index, values))
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
        },
        "only_left": [_row_view(key, record, barcodes) for key, record in only_left[:sample]],
        "only_right": [_row_view(key, record, barcodes) for key, record in only_right[:sample]],
        "changed": changed[:sample],
        "duplicate_keys": duplicates,
    }
    if blobs is not None:
        table["blobs"] = plan["blob_counts"]
    return table


def _file_row(
    table: str,
    change: str,
    key: Any,
    left_row: int | None,
    right_row: int | None,
    values: dict[str, Any],
) -> dict[str, Any]:
    return {
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
