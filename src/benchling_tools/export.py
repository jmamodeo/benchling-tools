"""Write a Benchling table or assay results to a local file and return a summary.

The file holds the rows. The return value is row count, column names, path, and
a SHA-256 checksum, so a large table does not enter an agent's context. Reads
follow every page. Benchling is not modified.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import os
import uuid
from pathlib import Path
from typing import Any

from . import listing
from .errors import NotFoundError, UpstreamError, ValidationError
from .redact import redact_text
from .services import (
    V2_STRUCTURED_TABLE_NAMESPACE,
    V2_STRUCTURED_TABLE_PREFIX,
    V3_DOCUMENT_PART_NAMESPACE,
    BenchlingService,
    field_definition_type,
)

_KINDS = {"table", "results-table", "assay-results"}
_FORMATS = {"json", "csv"}
_IDENTITY = ("id", "entryId", "schemaId", "projectId", "createdAt", "modifiedAt")
_COLUMN_KEYS = ("name", "systemName", "columnId")
_V2_STRUCTURED_TABLE_PREFIX = V2_STRUCTURED_TABLE_PREFIX
_V2_STRUCTURED_TABLE_NAMESPACE = V2_STRUCTURED_TABLE_NAMESPACE
_V3_DOCUMENT_PART_NAMESPACE = V3_DOCUMENT_PART_NAMESPACE
_NESTED_RESULT_KEYS = {"fields", "schemaFields"}
_REF_TYPENAMES = {"SchemaFieldDefinitionRef", "FieldDefinition"}


async def export_dataset(
    service: BenchlingService,
    *,
    kind: object,
    path: object,
    file_format: object,
    overwrite: object = False,
    part_id: object = None,
    entry: object = None,
    schema_id: object = None,
) -> dict[str, Any]:
    """Read one dataset, write it to ``path``, and return a summary.

    ``kind`` is ``table``, ``results-table``, or ``assay-results``. ``file_format``
    is ``json`` or ``csv``. Table and results-table exports require ``part_id``.
    Assay-result exports accept ``entry`` and ``schema_id`` and read every page.
    An existing file is left unchanged unless ``overwrite`` is true.
    """
    selected = _kind(kind)
    selected_format = _format(file_format)
    replace = _overwrite(overwrite)
    part = _optional(part_id, "part_id")
    entry_ref = _optional(entry, "entry")
    schema_ref = _optional(schema_id, "schema_id")
    _check_selector(selected, part, entry_ref, schema_ref)
    destination = _destination(path, overwrite=replace)
    if selected == "assay-results":
        columns, rows, source = await _load_assay_results(service, entry_ref, schema_ref)
    elif part is None:
        raise ValidationError("table and results-table exports require part_id")
    elif part.startswith(_V2_STRUCTURED_TABLE_PREFIX):
        columns, rows, source = await _load_listed_table(service, selected, part, entry_ref)
    elif selected == "results-table":
        columns, rows, source = await _load_results_table(service, part)
    else:
        columns, rows, source = await _load_table(service, part)
    payload = _render(selected, columns, rows, source, file_format=selected_format)
    _write(destination, payload, overwrite=replace)
    return {
        "kind": selected,
        "format": selected_format,
        "path": str(destination),
        "row_count": len(rows),
        "columns": columns,
        "checksum": hashlib.sha256(payload).hexdigest(),
        "checksum_algorithm": "sha256",
        "source": source,
    }


def _kind(value: object) -> str:
    key = value.strip().casefold().replace("_", "-") if isinstance(value, str) else ""
    if key in _KINDS:
        return key
    raise ValidationError(
        "kind must be table, results-table, or assay-results",
        details={"kind": value.strip() if isinstance(value, str) else None},
    )


def _format(value: object) -> str:
    key = value.strip().casefold() if isinstance(value, str) else ""
    if key in _FORMATS:
        return key
    raise ValidationError(
        "format must be json or csv",
        details={"format": value.strip() if isinstance(value, str) else None},
    )


def _overwrite(value: object) -> bool:
    if isinstance(value, bool):
        return value
    raise ValidationError("overwrite must be true or false")


def _optional(value: object, label: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise ValidationError(f"{label} must be a non-empty string")
    return value.strip()


def _check_selector(
    kind: str, part_id: str | None, entry: str | None, schema_id: str | None
) -> None:
    if kind in {"table", "results-table"}:
        listed = isinstance(part_id, str) and part_id.startswith(_V2_STRUCTURED_TABLE_PREFIX)
        if schema_id is not None or (entry is not None and not listed):
            raise ValidationError("entry and schema apply only to assay-results")
        return
    if part_id is not None:
        raise ValidationError("part_id applies only to table and results-table exports")


def _destination(value: object, *, overwrite: bool) -> Path:
    if not isinstance(value, str) or not value.strip():
        raise ValidationError("path must be a non-empty string")
    path = Path(value.strip()).expanduser().resolve()
    if path.exists() and not path.is_file():
        raise ValidationError("Export path is not a file", details={"path": str(path)})
    if path.exists() and not overwrite:
        raise ValidationError(
            "Refusing to overwrite an existing file without overwrite",
            details={"path": str(path)},
        )
    if not path.parent.is_dir():
        raise ValidationError("Export directory does not exist", details={"path": str(path)})
    return path


async def _load_table(
    service: BenchlingService, part_id: str
) -> tuple[list[str], list[dict[str, Any]], dict[str, Any]]:
    """Read every unstructured-table row page via the existing table read."""
    table = await service.get_table(part_id)
    content = table["content"]
    raw_rows = content["rows"]
    columns = _column_names(content.get("columnDefinitions"), _width(raw_rows))
    source: dict[str, Any] = {"part_id": str(table.get("id") or part_id)}
    _add_text(source, "name", content.get("name"))
    version = (table.get("versionMetadata") or {}).get("versionId")
    _add_text(source, "version_id", version)
    return columns, [_table_row(row, columns) for row in raw_rows], source


async def _load_results_table(
    service: BenchlingService, part_id: str
) -> tuple[list[str], list[dict[str, Any]], dict[str, Any]]:
    """Follow results-table row pages until next_token is absent."""
    first, collected = await _read_results_rows(service, part_id)
    columns = _column_names(first.get("column_definitions"), _width(collected))
    source: dict[str, Any] = {"part_id": str(first.get("id") or part_id)}
    _add_text(source, "name", first.get("name"))
    _add_text(source, "version_id", first.get("version_id"))
    _add_text(source, "schema_id", first.get("result_schema_id"))
    return columns, [_table_row(row, columns) for row in collected], source


async def _read_results_rows(
    service: BenchlingService, part_id: str
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """The first row page (table header) and every normalized row of a results table."""
    first = await service.list_results_table_rows(part_id, limit=listing.LIST_LIMIT_MAX)
    collected: list[dict[str, Any]] = list(first["items"])
    cursor = first["next_token"]
    while cursor:
        page = await service.list_results_table_rows(
            part_id, limit=listing.LIST_LIMIT_MAX, cursor=cursor
        )
        collected.extend(page["items"])
        cursor = page["next_token"]
    expected = first["row_count"]
    if expected != len(collected):
        raise UpstreamError(
            "Table row count did not match the rows that were returned",
            details={"part_id": part_id, "row_count": expected, "returned": len(collected)},
        )
    return first, collected


def _listed_table_error(
    part_id: str, *, kind: str, entry_id: str | None, on_entry: bool
) -> ValidationError:
    """validation_error that names the id namespace ``--part`` was given and the one it needs."""
    if kind == "table":
        message = (
            f"{part_id} is a {_V2_STRUCTURED_TABLE_NAMESPACE} id from `results tables`. "
            "`--kind table` expects a "
            f"{_V3_DOCUMENT_PART_NAMESPACE} id (docpart_…) from `entry inspect`. "
            "Export this results table with `--kind results-table --entry`."
        )
    elif entry_id is None:
        message = (
            f"{part_id} is a {_V2_STRUCTURED_TABLE_NAMESPACE} id from `results tables`. "
            "Pass --entry for that entry, or pass a "
            f"{_V3_DOCUMENT_PART_NAMESPACE} id (docpart_…) from `entry inspect`."
        )
    elif not on_entry:
        message = (
            f"{part_id} is a {_V2_STRUCTURED_TABLE_NAMESPACE} id and was not found on this entry. "
            "`results tables` lists those ids. A results-table export also accepts a "
            f"{_V3_DOCUMENT_PART_NAMESPACE} id (docpart_…) from `entry inspect`."
        )
    else:
        message = (
            f"{part_id} could not be resolved to a {_V3_DOCUMENT_PART_NAMESPACE} id, "
            "and the results table has no result schema to read."
        )
    details: dict[str, Any] = {
        "part_id": part_id,
        "id_namespace": _V2_STRUCTURED_TABLE_NAMESPACE,
        "expected_id_namespace": _V3_DOCUMENT_PART_NAMESPACE,
    }
    if entry_id is not None:
        details["entry_id"] = entry_id
    return ValidationError(message, details=details)


async def _load_listed_table(
    service: BenchlingService, kind: str, part_id: str, entry: str | None
) -> tuple[list[str], list[dict[str, Any]], dict[str, Any]]:
    """Export a ``strtbl_`` id from ``results tables``.

    ``--kind table`` only continues when an unstructured document part carries that
    handle. ``--kind results-table`` uses the entry's ``DocumentPartResultsTable``
    when one part matches, and otherwise reads assay results for the entry and the
    table's result schema. Benchling is only read.
    """
    if entry is None:
        raise _listed_table_error(part_id, kind=kind, entry_id=None, on_entry=False)
    resolved = await service.resolve_entry(entry)
    entry_id = str(resolved["entry_id"])
    if kind == "table":
        part = await service.document_part_for_listed_table(
            entry_id, {"table_id": part_id}, unstructured=True
        )
        if part is None:
            raise _listed_table_error(part_id, kind=kind, entry_id=entry_id, on_entry=True)
        columns, rows, source = await _load_table(service, part)
        source["table_id"] = part_id
        source["id_namespace"] = _V3_DOCUMENT_PART_NAMESPACE
        source["listed_id_namespace"] = _V2_STRUCTURED_TABLE_NAMESPACE
        return columns, rows, source
    try:
        note = await service.get_result_table(entry_id, part_id)
    except NotFoundError as exc:
        raise _listed_table_error(part_id, kind=kind, entry_id=entry_id, on_entry=False) from exc
    part = await service.document_part_for_listed_table(entry_id, note, unstructured=False)
    if part is not None:
        columns, rows, source = await _load_results_table(service, part)
        source["table_id"] = part_id
        source["id_namespace"] = _V3_DOCUMENT_PART_NAMESPACE
        source["listed_id_namespace"] = _V2_STRUCTURED_TABLE_NAMESPACE
        _add_text(source, "entry_id", entry_id)
        return columns, rows, source
    schema_id = note.get("schema_id")
    if not isinstance(schema_id, str) or not schema_id:
        raise _listed_table_error(part_id, kind=kind, entry_id=entry_id, on_entry=True)
    columns, rows, source = await _load_assay_results(service, entry_id, schema_id)
    source["part_id"] = part_id
    source["id_namespace"] = _V2_STRUCTURED_TABLE_NAMESPACE
    source["export_via"] = "assay_results"
    return columns, rows, source


async def _load_assay_results(
    service: BenchlingService, entry: str | None, schema_id: str | None
) -> tuple[list[str], list[dict[str, Any]], dict[str, Any]]:
    """Read every assay-result page for the optional entry and schema filters."""
    collected = await service.list_all_results(entry=entry, schema_id=schema_id)
    records = collected["items"]
    names, link_flags = await _schema_field_names(service, records, collected.get("schema_id"))
    columns = _assay_columns(records, names)
    source: dict[str, Any] = {}
    _add_text(source, "entry_id", collected.get("entry_id"))
    _add_text(source, "schema_id", collected.get("schema_id"))
    return (
        columns,
        [_assay_row(record, columns, names, link_flags) for record in records],
        source,
    )


def _add_text(source: dict[str, Any], key: str, value: object) -> None:
    if isinstance(value, str) and value:
        source[key] = value


def _width(rows: list[dict[str, Any]]) -> int:
    if not rows:
        return 0
    return max(len(row["cells"]) for row in rows)


def _column_names(definitions: object, width: int) -> list[str]:
    defs = definitions if isinstance(definitions, list) else []
    count = max(len(defs), width)
    names = [
        _column_name(defs[index] if index < len(defs) else None, index) for index in range(count)
    ]
    return _unique(names)


def _column_name(definition: object, index: int) -> str:
    if isinstance(definition, dict):
        for key in _COLUMN_KEYS:
            candidate = definition.get(key)
            if isinstance(candidate, str) and candidate.strip():
                return candidate.strip()
    return f"column_{index}"


def _unique(names: list[str]) -> list[str]:
    seen: dict[str, int] = {}
    unique: list[str] = []
    for name in names:
        count = seen.get(name, 0) + 1
        seen[name] = count
        unique.append(name if count == 1 else f"{name}__{count}")
    return unique


def _table_row(row: dict[str, Any], columns: list[str]) -> dict[str, Any]:
    values = [_cell_value(cell) for cell in row["cells"]]
    values.extend([None] * (len(columns) - len(values)))
    return {column: values[index] for index, column in enumerate(columns)}


def _cell_value(cell: dict[str, Any]) -> Any:
    """Prefer a computed evaluation, otherwise the value that was entered.

    An evaluation without a `value` (an unknown typename, or a
    `*CellEvaluationValue` such as an object link's `id` and `name`) falls back
    to `inputValue`, as the Benchling docs advise. A link or entity reference
    becomes its id, the same scalar assay-result export writes for that field.
    """
    evaluation = cell.get("evaluationValue")
    if isinstance(evaluation, dict) and evaluation.get("value") is not None:
        return _unwrap_field(evaluation["value"])
    entered = cell.get("inputValue")
    if isinstance(entered, dict) and "value" in entered:
        return _unwrap_field(entered["value"])
    return None


def _assay_columns(records: list[dict[str, Any]], names: dict[str, str] | None = None) -> list[str]:
    """Metadata scalars, then one column per schema field.

    V3 ``GET /result/items`` puts measurements in ``schemaFields``, an array of
    ``SchemaFieldValue`` (``fieldDefinition`` plus ``value``), not a top-level
    ``fields`` map. A V2 ``fields`` object is still accepted. Display names come
    from the field definition or from ``names`` (field-definition id to ``name``).
    """
    labels = names or {}
    present = {key for record in records for key in record}
    columns = [key for key in _IDENTITY if key in present]
    seen = set(columns)
    for record in records:
        for name in _field_pairs(record, labels):
            if name in seen:
                continue
            seen.add(name)
            columns.append(name)
    for record in records:
        for key, value in record.items():
            if key in seen or key in _NESTED_RESULT_KEYS or isinstance(value, dict | list):
                continue
            seen.add(key)
            columns.append(key)
    return columns


def _assay_row(
    record: dict[str, Any],
    columns: list[str],
    names: dict[str, str] | None = None,
    link_flags: dict[str, bool] | None = None,
) -> dict[str, Any]:
    values: dict[str, Any] = {}
    for name, value in _field_pairs(record, names or {}, link_flags).items():
        values.setdefault(name, value)
    row: dict[str, Any] = {}
    for column in columns:
        if column not in _IDENTITY and column in values:
            row[column] = values[column]
        else:
            row[column] = record.get(column)
    return row


def _field_pairs(
    record: dict[str, Any],
    names: dict[str, str],
    link_flags: dict[str, bool] | None = None,
) -> dict[str, Any]:
    """Schema-field columns in response order. Later duplicates do not replace earlier ones."""
    flags = link_flags or {}
    raw_id = record.get("id")
    owner_id = raw_id if isinstance(raw_id, str) and raw_id else None
    pairs: dict[str, Any] = {}
    raw = record.get("schemaFields")
    if isinstance(raw, list):
        for item in raw:
            if not isinstance(item, dict):
                continue
            name = _schema_field_name(item, names)
            if name and name not in pairs:
                pairs[name] = _schema_field_cell(item, owner_id=owner_id, link_flags=flags)
    elif isinstance(raw, dict):
        for key, value in raw.items():
            if isinstance(key, str) and key and key not in pairs:
                pairs[key] = _unwrap_field(value)
    fields = record.get("fields")
    if isinstance(fields, dict):
        for key, value in fields.items():
            if isinstance(key, str) and key and key not in pairs:
                pairs[key] = _unwrap_field(value)
    return pairs


def _schema_field_name(item: dict[str, Any], names: dict[str, str]) -> str | None:
    """Prefer the field's display name, then its system name, then its id."""
    definition = item.get("fieldDefinition")
    if isinstance(definition, str) and definition.strip():
        key = definition.strip()
        return names.get(key) or key
    if not isinstance(definition, dict):
        return None
    for key in ("name", "displayName"):
        candidate = definition.get(key)
        if isinstance(candidate, str) and candidate.strip():
            return candidate.strip()
    field_id = definition.get("id")
    if isinstance(field_id, str) and field_id.strip():
        mapped = names.get(field_id.strip())
        if mapped:
            return mapped
    system_name = definition.get("systemName")
    if isinstance(system_name, str) and system_name.strip():
        return system_name.strip()
    if isinstance(field_id, str) and field_id.strip():
        return field_id.strip()
    return None


def _schema_field_cell(
    item: dict[str, Any], *, owner_id: str | None, link_flags: dict[str, bool]
) -> Any:
    """Flatten a ``SchemaFieldValue``.

    A null value may still name another entity through ``linkedEntityId``. An
    unset field often carries the owning result's own id there, and that id is
    not a value. It is kept only for a link field, and only when it differs
    from ``owner_id``. A null value exports as null; an empty string stays empty.
    """
    if "value" in item:
        flattened = _unwrap_field(item["value"])
        if flattened is not None:
            return flattened
    else:
        flattened = None
    linked = item.get("linkedEntityId")
    if (
        isinstance(linked, str)
        and linked
        and linked != owner_id
        and _uses_linked_entity(item.get("fieldDefinition"), link_flags)
    ):
        return linked
    return flattened


def _uses_linked_entity(definition: object, link_flags: dict[str, bool]) -> bool:
    """True when this field is a link, or when its type is not known."""
    field_id = _definition_id(definition)
    if field_id is not None and field_id in link_flags:
        return link_flags[field_id]
    if isinstance(definition, dict):
        classified = _definition_is_link(definition)
        if classified is not None:
            return classified
    return True


def _definition_id(definition: object) -> str | None:
    if isinstance(definition, str) and definition.strip():
        return definition.strip()
    if isinstance(definition, dict):
        field_id = definition.get("id")
        if isinstance(field_id, str) and field_id.strip():
            return field_id.strip()
    return None


def _definition_is_link(definition: dict[str, Any]) -> bool | None:
    """True for a link field, False for another concrete type, None for a bare ref."""
    kind = field_definition_type(definition)
    if kind == "LINK" or kind.endswith("_LINK"):
        return True
    if kind != "UNKNOWN":
        return False
    typename = str(definition.get("__typename") or "").split(".")[-1]
    if not typename or typename in _REF_TYPENAMES:
        return None
    return False


def _unwrap_field(value: Any) -> Any:
    """Use a field's scalar value. Links and entity refs become their ids."""
    if isinstance(value, list):
        return [_unwrap_field(item) for item in value]
    if isinstance(value, dict):
        if "value" in value:
            return _unwrap_field(value["value"])
        if "textValue" in value:
            return value["textValue"]
        if "displayValue" in value:
            return value["displayValue"]
        linked = value.get("id")
        if isinstance(linked, str) and linked and ("__typename" in value or "type" in value):
            return linked
    return value


def _record_schema_id(record: dict[str, Any]) -> str | None:
    schema_id = record.get("schemaId")
    if isinstance(schema_id, str) and schema_id:
        return schema_id
    schema = record.get("schema")
    if isinstance(schema, dict):
        nested = schema.get("id")
        if isinstance(nested, str) and nested:
            return nested
    return None


def _record_needs_field_names(record: dict[str, Any]) -> bool:
    """True when a schemaFields entry has no display name of its own."""
    raw = record.get("schemaFields")
    if not isinstance(raw, list):
        return False
    for item in raw:
        if not isinstance(item, dict):
            continue
        definition = item.get("fieldDefinition")
        if isinstance(definition, str) and definition.strip():
            return True
        if not isinstance(definition, dict):
            continue
        name = definition.get("name")
        if isinstance(name, str) and name.strip():
            continue
        display = definition.get("displayName")
        if isinstance(display, str) and display.strip():
            continue
        if isinstance(definition.get("id"), str) and definition["id"].strip():
            return True
    return False


async def _schema_field_names(
    service: BenchlingService, records: list[dict[str, Any]], schema_id: object
) -> tuple[dict[str, str], dict[str, bool]]:
    """Map field-definition ids to display names, and record which ids are links.

    A missing schema is skipped. Other read failures propagate, so a schema outage
    is not written out as raw field ids. Link flags are empty when no schema was
    read. An unknown field may still use a linked id that is not the result id.
    """
    if not any(_record_needs_field_names(record) for record in records):
        return {}, {}
    schema_ids: list[str] = []
    if isinstance(schema_id, str) and schema_id:
        schema_ids.append(schema_id)
    for record in records:
        record_schema = _record_schema_id(record)
        if record_schema and record_schema not in schema_ids:
            schema_ids.append(record_schema)
    names: dict[str, str] = {}
    link_flags: dict[str, bool] = {}
    for sid in schema_ids:
        try:
            detail = await service.get_schema(sid)
        except NotFoundError:
            continue
        for definition in detail.get("field_definitions") or []:
            if not isinstance(definition, dict):
                continue
            field_id = definition.get("id")
            if not isinstance(field_id, str) or not field_id:
                continue
            if field_id not in link_flags:
                classified = _definition_is_link(definition)
                if classified is not None:
                    link_flags[field_id] = classified
            display = definition.get("name")
            if isinstance(display, str) and display.strip() and field_id not in names:
                names[field_id] = display.strip()
    return names, link_flags


def _render(
    kind: str,
    columns: list[str],
    rows: list[dict[str, Any]],
    source: dict[str, Any],
    *,
    file_format: str,
) -> bytes:
    if file_format == "json":
        document = {"kind": kind, "source": source, "columns": columns, "rows": rows}
        return (json.dumps(document, indent=2, ensure_ascii=False) + "\n").encode("utf-8")
    buffer = io.StringIO(newline="")
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(columns)
    for row in rows:
        writer.writerow([_csv_cell(row[column]) for column in columns])
    return buffer.getvalue().encode("utf-8")


def _csv_cell(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, str):
        return value
    if isinstance(value, int | float):
        return str(value)
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def _write(path: Path, payload: bytes, *, overwrite: bool) -> None:
    """Write ``payload`` atomically. ``os.link`` refuses to replace an existing file."""
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.partial")
    try:
        temporary.write_bytes(payload)
        if overwrite:
            os.replace(temporary, path)
        else:
            try:
                os.link(temporary, path)
            except FileExistsError as exc:
                raise ValidationError(
                    "Refusing to overwrite an existing file without overwrite",
                    details={"path": str(path)},
                ) from exc
    except ValidationError:
        raise
    except OSError as exc:
        raise ValidationError(
            "Could not write export file",
            details={"path": str(path), "reason": redact_text(str(exc))},
        ) from exc
    finally:
        temporary.unlink(missing_ok=True)
