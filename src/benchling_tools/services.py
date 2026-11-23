"""Read-side Benchling operations: entry resolution, snapshots, tables, schemas and results."""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import quote, urlparse
from uuid import UUID

from . import listing
from .api import BenchlingAPI
from .environment import protection_summary
from .errors import (
    AccessError,
    ConflictError,
    NotFoundError,
    UnsupportedError,
    UpstreamError,
    ValidationError,
)
from .models import CompletenessIssue, EntrySnapshot
from .redact import redact_text


def _error_text(exc: Exception) -> str:
    """User-visible error text with client secrets and tokens removed."""
    message = getattr(exc, "message", None)
    if isinstance(message, str):
        return redact_text(message)
    return redact_text(str(exc))


def _doctor_hint(exc: Exception, env: str) -> str:
    """One remediation line for a failed doctor check."""
    code = getattr(exc, "code", "")
    message = _error_text(exc).lower()
    login = f"benchling-tools auth login --env {env}"
    if (
        "not in the keychain" in message
        or "credentials are missing" in message
        or "no usable os keychain" in message
        or "no client secret" in message
    ):
        return f"Client secret is not in the keychain; run {login}"
    if (
        code == "authentication_error"
        or "authentication failed" in message
        or "client id" in message
    ):
        return f"Check the client ID and secret, then run {login}"
    if code == "access_denied":
        return "Check that this Benchling app is allowed to read the tenant."
    if code in {"upstream_error", "not_found"}:
        return "Check the tenant URL and that this machine can reach it over HTTPS."
    return f"Run benchling-tools doctor --env {env} again after fixing this check."


V2_STRUCTURED_TABLE_PREFIX = "strtbl_"
V2_STRUCTURED_TABLE_NAMESPACE = "v2_structured_table"
V3_DOCUMENT_PART_NAMESPACE = "v3_document_part"
V2_ASSAY_RESULT_NAMESPACE = "v2_assay_result"
V3_RESULT_NAMESPACE = "v3_result"
V3_RESULT_PREFIX = "result_"
UNKNOWN_ID_NAMESPACE = "unknown"
RESULT_ID_NAMESPACES = (V3_RESULT_NAMESPACE, V2_ASSAY_RESULT_NAMESPACE)
V3_RESULT_LOOKUP = "V3 GET /result/{result_id}"
V2_RESULT_LOOKUP = "V2 GET /assay-results:bulk-get"
V3_TO_V2_RESULT_LOOKUP = (
    "V3 GET /result/{result_id}, then V2 GET /assay-results by schemaId and createdAt"
)
UUID_ID = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)
ENTRY_ID = re.compile(r"^etr_[A-Za-z0-9]+$")
ENTRY_ID_IN_URL = re.compile(r"/(etr_[A-Za-z0-9]+)(?:[-/]|$)")
KNOWN_PART_TYPES = {
    "DocumentPartSection",
    "DocumentPartText",
    "DocumentPartHeader",
    "DocumentPartListItem",
    "DocumentPartCodeBlock",
    "DocumentPartUnstructuredTable",
    "DocumentPartStructuredTable",
    "DocumentPartResultsStructuredTable",
    "DocumentPartRegistrationStructuredTable",
    "DocumentPartInventoryStructuredTable",
    "DocumentPartAttachment",
    "SectionDocumentContent",
    "TextDocumentContent",
    "ListItemDocumentContent",
    "HeaderDocumentContent",
    "CodeBlockDocumentContent",
    "UnstructuredTableDocumentContentSummary",
    "ResultsStructuredTableDocumentContent",
    "RegistrationStructuredTableDocumentContent",
    "InventoryStructuredTableDocumentContent",
    "StructuredTableDocumentContent",
    "AttachmentDocumentContent",
    "AttachmentPlaceholderDocumentContent",
    "ImageGridDocumentContent",
    "BenchlingItemDocumentContent",
    "RunDocumentContent",
    "TextBoxDocumentContent",
    "DocumentPartTextBox",
    "DocumentPartResultsTable",
    "DocumentPartLookupTable",
    "DocumentPartUnavailable",
}

TABLE_ROW_PAGE_SIZE = 100
ENTRY_BULK_GET_LIMIT = 100
ASSAY_RESULT_BULK_GET_LIMIT = 200
RESULT_ID_FILTER_LIMIT = 100
CONTAINER_BARCODE_CHUNK = 100
V2_ID_LOOKUP_MAX_PAGES = 5
TEMPLATE_TEXT_PREVIEW_LIMIT = 160

_BENCHLING_VALUE_TYPES = {
    "TextValue": "text_value",
    "BooleanValue": "boolean_value",
    "DateValue": "date_value",
    "DateTimeValue": "datetime_value",
    "DecimalValue": "decimal_value",
    "IntegerValue": "integer_value",
    "JsonValue": "json_value",
    "ArrayValue": "array_value",
    "ObjectLinkValue": "object_link_value",
    "ObjectLinkListValue": "object_link_list_value",
    "TextAndUrlValue": "text_and_url_value",
}

_TABLE_CONTENT_KEYS = (
    "name",
    "isEvaluated",
    "caption",
    "resultSchema",
    "submissionStatus",
    "isSubmittable",
    "indentation",
)

_TABLE_ROW_KEYS = (
    "id",
    "rowIndex",
    "rowId",
    "submissionStatus",
    "isArchived",
    "isSubmittable",
)


def _require_part_id(value: object) -> str:
    """A document-part ID with surrounding whitespace removed."""
    if not isinstance(value, str) or not value.strip():
        raise ValidationError("part_id must be a non-empty string")
    return value.strip()


def entry_version(entry: dict[str, Any]) -> str:
    """Return the entry's version ID; raises UpstreamError if version metadata is absent."""
    metadata = entry.get("versionMetadata") or {}
    version = metadata.get("versionId") or metadata.get("id")
    if not version:
        raise UpstreamError(
            "Entry response did not include version metadata",
            details={"entry_id": entry.get("id")},
        )
    return str(version)


def part_content(part: dict[str, Any]) -> dict[str, Any]:
    """Support both legacy nested content and current flattened V3 parts.

    Original API objects remain unchanged in output; unknown types stay opaque.
    """
    content = part.get("content")
    return content if isinstance(content, dict) else part


def part_type(part: dict[str, Any]) -> str:
    """Return the part's __typename, or "Unknown" when absent."""
    content = part_content(part)
    return str(content.get("__typename") or "Unknown")


def is_section(part: dict[str, Any]) -> bool:
    """True for section parts, with or without a namespace prefix on the type."""
    return part_type(part).split(".")[-1] in {"SectionDocumentContent", "DocumentPartSection"}


def is_unstructured_table(part: dict[str, Any]) -> bool:
    """True for free-form (unstructured) table parts."""
    return (
        "UnstructuredTableDocumentContent" in part_type(part)
        or part_type(part) == "DocumentPartUnstructuredTable"
    )


def is_structured_table(part: dict[str, Any]) -> bool:
    """True for any structured table part (results, registration, inventory); excludes unstructured."""
    kind = part_type(part)
    return (
        "StructuredTableDocumentContent" in kind
        or kind.startswith("DocumentPart")
        and "StructuredTable" in kind
    ) and "Unstructured" not in kind


def is_results_table(part: dict[str, Any]) -> bool:
    """True for a first-class results table part."""
    return part_type(part).split(".")[-1] == "DocumentPartResultsTable"


def is_lookup_table(part: dict[str, Any]) -> bool:
    """True for a lookup table part. Its rows are read-only and carry no result records."""
    return part_type(part).split(".")[-1] == "DocumentPartLookupTable"


def is_text_box(part: dict[str, Any]) -> bool:
    """True for a labeled fill-in text box part."""
    return part_type(part).split(".")[-1] in {"DocumentPartTextBox", "TextBoxDocumentContent"}


def text_box_detail(part: dict[str, Any]) -> dict[str, Any]:
    """A text box's label, full text, rich-text spans and submission state."""
    content = part_content(part)
    return {
        "part_id": part.get("id"),
        "name": part_display_name(part),
        "text": content.get("text"),
        "formats": content.get("formats") or [],
        "links": content.get("links") or [],
        "mentions": content.get("mentions") or [],
        "submission_status": content.get("submissionStatus"),
        "is_submittable": content.get("isSubmittable"),
    }


def part_version_id(part: dict[str, Any]) -> str | None:
    """The entry version a document part was read at, when the part carries one."""
    metadata = part.get("versionMetadata")
    version = part.get("versionId") or (
        metadata.get("versionId") if isinstance(metadata, dict) else None
    )
    return str(version) if version else None


_TABLE_HANDLE_KEYS = ("apiId", "tableHandleId", "structuredTableId", "legacyId", "v2Id")


def table_handle_ids(part: dict[str, Any]) -> set[str]:
    """Ids that can refer to the same table on a V2 note and a V3 document part."""
    found: set[str] = set()
    part_id = part.get("id")
    if isinstance(part_id, str) and part_id:
        found.add(part_id)
    content = part_content(part)
    for source in (part, content):
        for key in _TABLE_HANDLE_KEYS:
            value = source.get(key)
            if isinstance(value, str) and value:
                found.add(value)
    return found


def part_display_name(part: dict[str, Any]) -> str | None:
    """The part's display name, from flattened or legacy content."""
    content = part_content(part)
    for source in (content, part):
        name = source.get("name")
        if isinstance(name, str) and name:
            return name
    return None


def part_result_schema_id(part: dict[str, Any]) -> str | None:
    """The result schema id on a results-table part, when the part carries one."""
    content = part_content(part)
    for source in (content, part):
        schema = source.get("resultSchema")
        if isinstance(schema, dict):
            schema_id = schema.get("id")
            if isinstance(schema_id, str) and schema_id:
                return schema_id
        elif isinstance(schema, str) and schema:
            return schema
    return None


def is_unavailable_part(part: dict[str, Any]) -> bool:
    """True when Benchling withheld the part's concrete type at this API stability."""
    return part_type(part).split(".")[-1] == "DocumentPartUnavailable"


def rows_items_path(base_url: str, part_id: str, rows_url: Any, *, collection: str) -> str:
    """Return the `/rows/items` path for a table part, rejecting any other URL."""
    expected = f"/{collection}/{part_id}/rows/items"
    if not isinstance(rows_url, str) or not rows_url:
        raise UpstreamError("Table response contained invalid rows", details={"part_id": part_id})
    parsed = urlparse(rows_url)
    if parsed.scheme or parsed.netloc:
        if parsed.scheme != "https" or parsed.netloc != urlparse(base_url).netloc:
            raise UpstreamError(
                "Table rows URL does not belong to this tenant",
                details={"part_id": part_id},
            )
        path = parsed.path
    else:
        path = parsed.path or rows_url.split("?", 1)[0]
    marker = "/api/v3"
    index = path.find(marker)
    if index != -1:
        path = path[index + len(marker) :]
    if not path.startswith("/"):
        path = f"/{path}"
    if path != expected:
        raise UpstreamError("Table response contained invalid rows", details={"part_id": part_id})
    return path


_CELL_ERROR_KEYS = ("errorType", "message", "type")


def _invalid_rows(**context: Any) -> UpstreamError:
    """The invalid-rows error, with only locating context (never a cell value) in details."""
    details = {key: value for key, value in context.items() if value is not None}
    return UpstreamError("Table response contained invalid rows", details=details)


def _typename_of(value: Any) -> str | None:
    typename = value.get("__typename") if isinstance(value, dict) else None
    return typename if isinstance(typename, str) else None


def normalize_benchling_value(value: Any, *, require_text: bool) -> dict[str, Any]:
    """Map a Benchling value object (`__typename` plus `value`) onto `{type, value}`.

    `value` is optional on every BenchlingValue type, so a missing one is `null`.
    """
    typename = _typename_of(value)
    if not isinstance(value, dict):
        raise _invalid_rows()
    kind = _BENCHLING_VALUE_TYPES.get(typename) if typename is not None else None
    if kind is None or (require_text and kind != "text_value"):
        raise _invalid_rows(typename=typename)
    if require_text and not isinstance(value.get("value"), str):
        raise _invalid_rows(typename=typename)
    normalized: dict[str, Any] = {"type": kind, "value": value.get("value")}
    if kind == "text_and_url_value" and "url" in value:
        normalized["url"] = value["url"]
    if kind == "decimal_value" and "numericValue" in value:
        normalized["numericValue"] = value["numericValue"]
    return normalized


def normalize_evaluation_value(value: Any) -> dict[str, Any]:
    """Normalize a cell's `evaluationValue` without failing on shapes the toolkit does not know.

    Known BenchlingValue types map as for `inputValue`. A `*CellEvaluationValue`
    (for example ObjectLinkCellEvaluationValue with `id` and `name`) keeps its
    keys under `type: <typename>`. Any other typename becomes
    `{type: "unknown", typename}`; readers then fall back to `inputValue`, as the
    Benchling docs advise.
    """
    if not isinstance(value, dict):
        raise _invalid_rows()
    typename = _typename_of(value)
    if typename is not None and typename in _BENCHLING_VALUE_TYPES:
        return normalize_benchling_value(value, require_text=False)
    if typename is not None and typename.split(".")[-1].endswith("CellEvaluationValue"):
        normalized: dict[str, Any] = {"type": typename}
        normalized.update({key: item for key, item in value.items() if key != "__typename"})
        return normalized
    return {"type": "unknown", "typename": typename}


def _cell_errors(errors: Any) -> list[dict[str, Any]] | None:
    """The cell's CellError list (errorType, message, type), when Benchling sent one."""
    if not isinstance(errors, list):
        return None
    return [
        {key: error[key] for key in _CELL_ERROR_KEYS if key in error}
        for error in errors
        if isinstance(error, dict)
    ]


def normalize_table_cell(
    cell: Any,
    *,
    require_text: bool,
    part_id: str | None = None,
    row_index: int | None = None,
    cell_index: int | None = None,
) -> dict[str, Any]:
    """Normalize one table cell. Unstructured cells must carry a text input.

    A rejected cell raises with part, row, cell and typename in `details`, never its values.
    """
    location = {"part_id": part_id, "row_index": row_index, "cell_index": cell_index}
    if not isinstance(cell, dict):
        raise _invalid_rows(**location)
    normalized: dict[str, Any] = {}
    if cell.get("inputValue") is not None:
        try:
            normalized["inputValue"] = normalize_benchling_value(
                cell["inputValue"], require_text=require_text
            )
        except UpstreamError:
            raise _invalid_rows(**location, typename=_typename_of(cell["inputValue"])) from None
    elif require_text:
        raise _invalid_rows(**location)
    evaluation = cell.get("evaluationValue")
    if evaluation is not None:
        try:
            normalized["evaluationValue"] = normalize_evaluation_value(evaluation)
        except UpstreamError:
            raise _invalid_rows(**location, typename=_typename_of(evaluation)) from None
    for key in ("format", "mergeRange", "mergeRangeParent", "selectorId"):
        if cell.get(key) is not None:
            normalized[key] = cell[key]
    errors = _cell_errors(cell.get("errors"))
    if errors:
        normalized["errors"] = errors
    return normalized


def normalize_table_row(
    row: Any,
    *,
    require_text: bool,
    part_id: str | None = None,
    position: int | None = None,
) -> dict[str, Any]:
    """Normalize one table row and its cells.

    `position` is the row's place in the read; it names the row in an error
    when the row has no valid `rowIndex`.
    """
    raw_index = row.get("rowIndex") if isinstance(row, dict) else None
    valid_index = isinstance(raw_index, int) and not isinstance(raw_index, bool) and raw_index >= 0
    location: dict[str, Any] = {
        "part_id": part_id,
        "row_index": raw_index if valid_index else position,
    }
    if not isinstance(row, dict):
        raise _invalid_rows(**location, check="row")
    if not isinstance(row.get("cells"), list):
        raise _invalid_rows(**location, check="cells")
    for key in ("id", "rowId"):
        if key in row and row[key] is not None and (not isinstance(row[key], str) or not row[key]):
            raise _invalid_rows(**location, check=key)
    if raw_index is not None and not valid_index:
        raise _invalid_rows(**location, check="rowIndex")
    for key in ("isArchived", "isSubmittable"):
        if key in row and row[key] is not None and not isinstance(row[key], bool):
            raise _invalid_rows(**location, check=key)
    status = row.get("submissionStatus")
    if status is not None and (
        not isinstance(status, str)
        or status
        not in {
            "UNSUBMITTED",
            "SUBMITTED",
            "HAS_UNSUBMITTED_CHANGES",
            "UNEVALUATED",
        }
    ):
        raise _invalid_rows(**location, check="submissionStatus")
    # The schema type is ResultRef, but Benchling sends the concrete typename
    # (for example "Result"), so `__typename` is informational.
    result = row.get("result")
    if result is not None and (
        not isinstance(result, dict)
        or not isinstance(result.get("id"), str)
        or not result["id"]
        or (result.get("__typename") is not None and not isinstance(result["__typename"], str))
    ):
        raise _invalid_rows(**location, check="result")
    normalized: dict[str, Any] = {
        "cells": [
            normalize_table_cell(cell, require_text=require_text, cell_index=index, **location)
            for index, cell in enumerate(row["cells"])
        ]
    }
    for key in _TABLE_ROW_KEYS:
        if key in row and row[key] is not None:
            normalized[key] = row[key]
    if row.get("result") is not None:
        normalized["result"] = row["result"]
    return normalized


def table_content_from_detail(
    detail: dict[str, Any], rows: list[dict[str, Any]], *, fallback: str
) -> dict[str, Any]:
    """Build the snapshot content object from a table-part response and its rows."""
    content: dict[str, Any] = {
        "__typename": str(detail.get("__typename") or fallback),
        "columnDefinitions": detail.get("columnDefinitions") or [],
        "rows": rows,
    }
    for key in _TABLE_CONTENT_KEYS:
        if key in detail:
            content[key] = detail[key]
    return content


def field_definition_type(definition: dict[str, Any]) -> str:
    """Return a stable field type across the V3 union's response variants."""
    explicit = definition.get("type")
    if explicit:
        return str(explicit).upper()
    typename = str(definition.get("__typename") or "").split(".")[-1]
    primitive = {
        "TextFieldDefinition": "TEXT",
        "LongTextFieldDefinition": "LONG_TEXT",
        "FloatFieldDefinition": "FLOAT",
        "DecimalFieldDefinition": "DECIMAL",
        "IntegerFieldDefinition": "INTEGER",
        "BooleanFieldDefinition": "BOOLEAN",
        "DateFieldDefinition": "DATE",
        "DateTimeFieldDefinition": "DATETIME",
        "JsonFieldDefinition": "JSON",
        "DropdownLinkFieldDefinition": "DROPDOWN",
    }
    if typename in primitive:
        return primitive[typename]
    if typename.endswith("LinkFieldDefinition"):
        return "LINK"
    return "UNKNOWN"


def link_kind(definition: dict[str, Any]) -> str:
    """Distinguish links that do not carry a schema constraint."""
    typename = str(definition.get("__typename") or "").split(".")[-1]
    return {
        "StorableLinkFieldDefinition": "storable",
        "AnyEntityLinkFieldDefinition": "any_entity",
        "EntryLinkFieldDefinition": "entry",
        "RequestLinkFieldDefinition": "request",
        "SystemCategoryLinkFieldDefinition": "system_category",
        "AttachmentLinkFieldDefinition": "attachment",
        "DropdownLinkFieldDefinition": "dropdown",
    }.get(typename, "schema_constrained")


_ATTACHMENT_TYPENAMES = {"AttachmentLinkFieldDefinition", "BlobLinkFieldDefinition"}
_ATTACHMENT_TYPES = {"ATTACHMENT_LINK", "BLOB_LINK"}


def is_attachment_field(definition: dict[str, Any]) -> bool:
    """True for a field whose value is the ID of an uploaded blob (attachment)."""
    explicit = definition.get("type")
    if explicit:
        return str(explicit).upper() in _ATTACHMENT_TYPES
    typename = str(definition.get("__typename") or "").split(".")[-1]
    return typename in _ATTACHMENT_TYPENAMES


def build_sections(parts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Group a flat part list into sections; each records its index range and child part IDs.

    Parts before the first section belong to no section.
    """
    sections: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None
    for index, part in enumerate(parts):
        if is_section(part):
            if current:
                current["end_index"] = index - 1
            content = part_content(part)
            current = {
                "part_id": part.get("id"),
                "title": content.get("title"),
                "date": content.get("date"),
                "start_index": index,
                "end_index": index,
                "child_part_ids": [],
            }
            sections.append(current)
        elif current:
            current["child_part_ids"].append(part.get("id"))
            current["end_index"] = index
    return sections


def text_box_summaries(
    parts: list[dict[str, Any]], sections: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Each text box's label, full text and submission state, with the section that holds it."""
    section_of: dict[Any, dict[str, Any]] = {}
    for section in sections:
        reference = {"part_id": section.get("part_id"), "title": section.get("title")}
        for child in section.get("child_part_ids") or []:
            section_of[child] = reference
    summaries = []
    for part in parts:
        if is_text_box(part):
            detail = text_box_detail(part)
            summaries.append(
                {
                    "part_id": detail["part_id"],
                    "name": detail["name"],
                    "text": detail["text"],
                    "submission_status": detail["submission_status"],
                    "is_submittable": detail["is_submittable"],
                    "section": section_of.get(part.get("id")),
                }
            )
    return summaries


def lookup_table_summaries(
    parts: list[dict[str, Any]], tables: dict[str, dict[str, Any]]
) -> list[dict[str, Any]]:
    """Each lookup table's part ID, name and row count."""
    summaries = []
    for part in parts:
        part_id = part.get("id")
        table = tables.get(part_id) if isinstance(part_id, str) else None
        if is_lookup_table(part) and table is not None:
            content = part_content(table)
            summaries.append(
                {
                    "part_id": part_id,
                    "name": part_display_name(part) or content.get("name"),
                    "row_count": table.get("rowCount", len(content.get("rows") or [])),
                }
            )
    return summaries


def result_id_namespace(result_id: str) -> str:
    """``v3_result`` for a ``result_…`` id, ``v2_assay_result`` for a UUID, else ``unknown``."""
    if result_id.startswith(V3_RESULT_PREFIX):
        return V3_RESULT_NAMESPACE
    if UUID_ID.match(result_id):
        return V2_ASSAY_RESULT_NAMESPACE
    return UNKNOWN_ID_NAMESPACE


def assay_result_lookup(result_id: str) -> dict[str, Any]:
    """How ``get_assay_results`` reads one id: its namespace and the lookup path used."""
    namespace = result_id_namespace(result_id)
    v3 = namespace == V3_RESULT_NAMESPACE
    return {
        "result_id": result_id,
        "id_namespace": namespace,
        "expected_id_namespace": V3_RESULT_NAMESPACE if v3 else V2_ASSAY_RESULT_NAMESPACE,
        "lookup": V3_TO_V2_RESULT_LOOKUP if v3 else V2_RESULT_LOOKUP,
    }


def _result_not_found(
    result_id: str, *, expected: str, lookup: str, reason: str = "was not found"
) -> NotFoundError:
    namespace = result_id_namespace(result_id)
    passed = {
        V3_RESULT_NAMESPACE: f"a {V3_RESULT_NAMESPACE} id (result_…)",
        V2_ASSAY_RESULT_NAMESPACE: f"a {V2_ASSAY_RESULT_NAMESPACE} id (a UUID)",
    }.get(namespace, "neither a V3 result id (result_…) nor a V2 assay result UUID")
    return NotFoundError(
        f"Result {result_id} {reason}. The id passed is {passed}; it was read as a "
        f"{expected} id with {lookup}. Both V3 result ids and V2 assay result UUIDs are accepted.",
        details={
            "result_id": result_id,
            "id_namespace": namespace,
            "expected_id_namespace": expected,
            "lookup": lookup,
            "accepted_id_namespaces": list(RESULT_ID_NAMESPACES),
        },
    )


def _result_schema_id(record: dict[str, Any]) -> str | None:
    schema = record.get("schema")
    if isinstance(schema, dict) and isinstance(schema.get("id"), str):
        return str(schema["id"])
    schema_id = record.get("schemaId")
    return schema_id if isinstance(schema_id, str) and schema_id else None


def _result_created_at(record: dict[str, Any]) -> datetime | None:
    value = record.get("createdAt")
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _rfc3339(value: datetime) -> str:
    return value.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def with_result_ids(record: dict[str, Any], v2_id: Any, v3_id: Any) -> dict[str, Any]:
    """The record with ``v2_id`` (V2 assay result UUID) and ``v3_id`` (``result_…``) added."""
    return {
        **record,
        "v2_id": v2_id if isinstance(v2_id, str) and v2_id else None,
        "v3_id": v3_id if isinstance(v3_id, str) and v3_id else None,
    }


def _lift_invalid_barcodes(exc: ValidationError) -> None:
    """Copy a V2 400's error.invalidBarcodes to details.invalid_barcodes, with a hint."""
    response = exc.details.get("response")
    error = response.get("error") if isinstance(response, dict) else None
    invalid = error.get("invalidBarcodes") if isinstance(error, dict) else None
    if not isinstance(invalid, list):
        return
    barcodes = [item for item in invalid if isinstance(item, str)]
    if barcodes:
        exc.details["invalid_barcodes"] = barcodes
        exc.details["hint"] = "Benchling did not recognise these barcodes: " + ", ".join(barcodes)


def _container_summary(barcode: str, container: dict[str, Any]) -> dict[str, Any]:
    """The fields a barcode preflight reads: container, schema, contents, archived."""
    contents: list[dict[str, Any]] = []
    for content in container.get("contents") or []:
        if not isinstance(content, dict):
            continue
        batch = content.get("batch") if isinstance(content.get("batch"), dict) else None
        entity = content.get("entity") if isinstance(content.get("entity"), dict) else None
        if entity is None and batch is not None and isinstance(batch.get("entity"), dict):
            entity = batch["entity"]
        contents.append(
            {
                "entity_id": entity.get("id") if entity else None,
                "entity_registry_id": entity.get("entityRegistryId") if entity else None,
                "batch_id": batch.get("id") if batch else None,
            }
        )
    schema = container.get("schema")
    return {
        "barcode": barcode,
        "container_id": container.get("id"),
        "schema_id": schema.get("id") if isinstance(schema, dict) else None,
        "contents": contents,
        "archived": container.get("archiveRecord") is not None,
    }


class BenchlingService:
    """Read operations built on BenchlingAPI that return plain JSON-ready dicts."""

    def __init__(self, api: BenchlingAPI) -> None:
        self.api = api

    async def resolve_entry(self, reference: str) -> dict[str, Any]:
        """Resolve an entry ID, tenant URL or display ID to its canonical ID.

        Raises ValidationError for a foreign-tenant URL or ambiguous display ID,
        and NotFoundError when nothing matches.
        """
        reference = reference.strip()
        parsed_id: str | None = None
        if ENTRY_ID.fullmatch(reference):
            parsed_id = reference
        elif reference.startswith("https://"):
            parsed = urlparse(reference)
            if parsed.netloc != urlparse(self.api.environment.base_url).netloc:
                raise ValidationError(
                    "Entry URL belongs to a different tenant",
                    details={"environment_tenant": self.api.environment.base_url, "url": reference},
                )
            match = ENTRY_ID_IN_URL.search(parsed.path)
            parsed_id = match.group(1) if match else None
            if not parsed_id:
                raise ValidationError("URL does not contain a Benchling entry ID")
        if parsed_id:
            entry = await self.api.request("GET", f"/entry/{parsed_id}")
            return {
                "input": reference,
                "entry_id": entry.get("id", parsed_id),
                "display_id": entry.get("displayId"),
                "name": entry.get("name"),
                "web_url": listing.entry_web_url(entry),
                "resolved_by": "canonical_id",
            }

        matches = [
            item
            async for item in self.api.paginate(
                "/entries",
                version=2,
                params={"displayIds": reference, "pageSize": 100},
                item_key="entries",
            )
        ]
        exact = [item for item in matches if item.get("displayId") == reference]
        if len(exact) > 1:
            raise ValidationError(
                "Entry display ID is ambiguous",
                details={"display_id": reference, "entry_ids": [x.get("id") for x in exact]},
            )
        if not exact:
            raise NotFoundError(
                "No entry matches the supplied ID",
                details={"reference": reference},
            )
        found = exact[0]
        return {
            "input": reference,
            "entry_id": found["id"],
            "display_id": found.get("displayId"),
            "name": found.get("name"),
            "web_url": listing.entry_web_url(found),
            "resolved_by": "display_id",
        }

    async def get_entry(self, reference: str, *, full: bool = False) -> dict[str, Any]:
        """Fetch an entry; with full=True assemble a snapshot of parts, tables and schema.

        A full snapshot is retried once if the entry version changes mid-read,
        then raises ConflictError.
        """
        resolved = await self.resolve_entry(reference)
        entry_id = resolved["entry_id"]
        if not full:
            entry = await self.api.request("GET", f"/entry/{entry_id}")
            return {
                "resolved": resolved,
                "entry": entry,
                "version_id": entry_version(entry),
                "web_url": listing.entry_web_url(entry),
            }
        for attempt in range(2):
            first = await self.api.request("GET", f"/entry/{entry_id}")
            version = entry_version(first)
            snapshot = await self._assemble_snapshot(first, version)
            last = await self.api.request("GET", f"/entry/{entry_id}")
            final_version = entry_version(last)
            if final_version == version:
                return {
                    "resolved": resolved,
                    **snapshot.model_dump(mode="json", by_alias=True),
                    "web_url": listing.entry_web_url(last) or listing.entry_web_url(first),
                }
            if attempt == 1:
                raise ConflictError(
                    "Entry changed while the complete snapshot was being assembled",
                    details={
                        "entry_id": entry_id,
                        "initial_version": version,
                        "final_version": final_version,
                    },
                )
        raise AssertionError("unreachable")

    async def get_entries(
        self,
        *,
        entry_ids: object = None,
        display_ids: object = None,
    ) -> dict[str, Any]:
        """Fetch entries by canonical IDs or display IDs using the V2 bulk-get endpoint."""
        requested_ids = self._bulk_lookup_ids(entry_ids, "entry_ids")
        requested_display_ids = self._bulk_lookup_ids(display_ids, "display_ids")
        if (requested_ids is None) == (requested_display_ids is None):
            raise ValidationError("Specify exactly one of entry_ids or display_ids")
        requested = requested_ids if requested_ids is not None else requested_display_ids or []
        parameter = "entryIds" if requested_ids is not None else "displayIds"
        entries: list[dict[str, Any]] = []
        for offset in range(0, len(requested), ENTRY_BULK_GET_LIMIT):
            batch = requested[offset : offset + ENTRY_BULK_GET_LIMIT]
            payload = await self.api.request(
                "GET",
                "/entries:bulk-get",
                version=2,
                params={parameter: ",".join(batch)},
            )
            response_entries = payload.get("entries")
            if not isinstance(response_entries, list) or any(
                not isinstance(item, dict) for item in response_entries
            ):
                raise UpstreamError("Benchling returned an unexpected bulk entry response")
            entries.extend(
                {**item, "web_url": listing.entry_web_url(item)} for item in response_entries
            )
        return {"items": entries, "count": len(entries)}

    @staticmethod
    def _bulk_lookup_ids(value: object, label: str) -> list[str] | None:
        """Validate one repeated CLI/MCP list before joining it for V2."""
        if value is None:
            return None
        if isinstance(value, str) or not isinstance(value, (list, tuple)):
            raise ValidationError(f"{label} must be a non-empty list of strings")
        values = [BenchlingService._container_reference(item, label) for item in value]
        if not values:
            raise ValidationError(f"{label} must be a non-empty list of strings")
        if any("," in item for item in values):
            raise ValidationError(f"{label} items must not contain commas")
        return values

    async def get_folder(self, folder_id: str) -> dict[str, Any]:
        """Return one V3 folder by ID."""
        folder_id = self._container_reference(folder_id, "folder_id")
        folder = await self.api.request("GET", f"/folder/{quote(folder_id, safe='')}")
        if not isinstance(folder, dict) or not folder.get("id"):
            raise UpstreamError("Benchling returned an unexpected folder response")
        return folder

    async def get_template(self, template_id: str) -> dict[str, Any]:
        """Return entry-template metadata and a compact summary of its V2 note structure."""
        template_id = self._container_reference(template_id, "template_id")
        payload = await self.api.request(
            "GET", f"/entry-templates/{quote(template_id, safe='')}", version=2
        )
        template = self._v2_object(payload, "entryTemplate")
        return {
            "template_id": template["id"],
            "name": template.get("name"),
            "schema_id": (template.get("schema") or {}).get("id")
            if isinstance(template.get("schema"), dict)
            else None,
            "modified_at": template.get("modifiedAt"),
            "structure_source": "V2 is used because V3 EntryTemplate.Get returns metadata only",
            "structure": self._summarize_template(template),
        }

    @staticmethod
    def _summarize_template(template: dict[str, Any]) -> dict[str, Any]:
        days = template.get("days") or []
        if not isinstance(days, list) or any(not isinstance(day, dict) for day in days):
            raise UpstreamError("Benchling returned an unexpected entry template structure")
        note_types: list[str] = []
        results_table_schema_ids: list[str] = []
        table_names: list[str] = []
        section_titles: list[str] = []
        text_previews: list[dict[str, str]] = []
        text_types = {"text", "code", "text_box"}
        text_block_count = 0
        for day in days:
            title = day.get("title")
            if isinstance(title, str) and title:
                section_titles.append(title)
            notes = day.get("notes") or []
            if not isinstance(notes, list) or any(not isinstance(note, dict) for note in notes):
                raise UpstreamError("Benchling returned an unexpected entry template structure")
            for note in notes:
                note_type = note.get("type")
                if not isinstance(note_type, str) or not note_type:
                    raise UpstreamError("Benchling returned an entry template note without a type")
                note_types.append(note_type)
                if note_type == "results_table":
                    schema_id = note.get("assayResultSchemaId")
                    if isinstance(schema_id, str) and schema_id:
                        results_table_schema_ids.append(schema_id)
                table_name = note.get("name")
                if not isinstance(table_name, str) or not table_name:
                    table = note.get("table")
                    table_name = table.get("name") if isinstance(table, dict) else None
                if isinstance(table_name, str) and table_name:
                    table_names.append(table_name)
                text = note.get("text")
                if note_type in text_types:
                    text_block_count += 1
                    if isinstance(text, str) and text.strip():
                        normalized = " ".join(text.split())
                        preview = normalized[:TEMPLATE_TEXT_PREVIEW_LIMIT]
                        if len(normalized) > TEMPLATE_TEXT_PREVIEW_LIMIT:
                            preview += "…"
                        text_previews.append({"type": note_type, "preview": preview})
        return {
            "note_types": note_types,
            "results_table_schema_ids": results_table_schema_ids,
            "table_names": table_names,
            "section_titles": section_titles,
            "text_block_count": text_block_count,
            "text_previews": text_previews,
        }

    async def list_templates(
        self,
        *,
        name: object = None,
        limit: object = listing.LIST_LIMIT,
        cursor: object = None,
    ) -> dict[str, Any]:
        """Return one page of entry templates, optionally filtered by exact name."""
        page_size = listing.check_limit(limit)
        if name is not None and (not isinstance(name, str) or not name.strip()):
            raise ValidationError("name must be a non-empty string")
        if cursor is not None and (not isinstance(cursor, str) or not cursor):
            raise ValidationError("cursor must be a non-empty string")
        params: dict[str, Any] = {"pageSize": page_size}
        if name is not None:
            params["name"] = name.strip()
        if cursor is not None:
            params["nextToken"] = cursor
        payload = await self.api.request("GET", "/entry-templates", version=2, params=params)
        templates = payload.get("entryTemplates")
        if not isinstance(templates, list) or any(not isinstance(item, dict) for item in templates):
            raise UpstreamError("Benchling returned an unexpected entry template list")
        next_token = payload.get("nextToken")
        if next_token is not None and not isinstance(next_token, str):
            raise UpstreamError("Benchling returned an invalid entry template pagination token")
        return {
            "items": [
                {
                    "id": item.get("id"),
                    "name": item.get("name"),
                    "modified_at": item.get("modifiedAt"),
                }
                for item in templates
            ],
            "next_token": next_token or None,
        }

    async def inspect_entry(self, reference: str) -> dict[str, Any]:
        """Summarize a full entry snapshot: sections, part summaries, table sizes and completeness."""
        snapshot = await self.get_entry(reference, full=True)
        return {
            "resolved": snapshot["resolved"],
            "entry": snapshot["entry"],
            "version_id": snapshot["version_id"],
            "project_id": (snapshot.get("fields") or {}).get("project_id"),
            "result_tables": snapshot.get("result_tables", []),
            "sections": snapshot["sections"],
            "parts": [
                {
                    "id": part.get("id"),
                    "type": part_type(part),
                    "summary": part_content(part),
                }
                for part in snapshot["parts"]
            ],
            "tables": {
                key: {
                    "part_id": key,
                    "type": value.get("content", {}).get("__typename"),
                    "name": value.get("content", {}).get("name"),
                    "row_count": len(value.get("content", {}).get("rows", [])),
                }
                for key, value in snapshot["tables"].items()
            },
            "text_boxes": text_box_summaries(snapshot["parts"], snapshot["sections"]),
            "lookup_tables": lookup_table_summaries(snapshot["parts"], snapshot["tables"]),
            "schema": snapshot.get("schema"),
            "completeness": snapshot["completeness"],
            "issues": snapshot["issues"],
        }

    async def get_section(self, reference: str, section: str) -> dict[str, Any]:
        """Return one section's parts and tables, matched by part ID or title.

        Raises NotFoundError if none match and ValidationError if the title is ambiguous.
        """
        snapshot = await self.get_entry(reference, full=True)
        matches = [
            item
            for item in snapshot["sections"]
            if item.get("part_id") == section or item.get("title") == section
        ]
        if not matches:
            raise NotFoundError("Section was not found", details={"section": section})
        if len(matches) > 1:
            raise ValidationError(
                "Section title is ambiguous; use its part ID",
                details={"section": section, "part_ids": [x["part_id"] for x in matches]},
            )
        target = matches[0]
        ids = {target["part_id"], *target["child_part_ids"]}
        parts = [part for part in snapshot["parts"] if part.get("id") in ids]
        tables = {key: value for key, value in snapshot["tables"].items() if key in ids}
        return {
            "entry_id": snapshot["resolved"]["entry_id"],
            "version_id": snapshot["version_id"],
            "section": target,
            "parts": parts,
            "tables": tables,
            "text_boxes": text_box_summaries(parts, [target]),
            "lookup_tables": lookup_table_summaries(parts, tables),
        }

    async def get_part(self, reference: str, part_id: str) -> dict[str, Any]:
        """Return one document part and its detail; raises NotFoundError if absent.

        Reads only that part with `GET /entry/{id}/parts/items?id.anyOf=…`, then the
        part's table detail and rows when it is a table. When Benchling rejects the
        `id.anyOf` filter, it falls back to a full entry snapshot. A text box adds
        `text_box`. The read is retried once if the entry version changes between
        the part and its table, then raises ConflictError.
        """
        part_id = _require_part_id(part_id)
        resolved = await self.resolve_entry(reference)
        entry_id = str(resolved["entry_id"])
        for attempt in range(2):
            try:
                part = await self._entry_part(entry_id, part_id)
            except ValidationError:
                return await self._get_part_from_snapshot(reference, part_id)
            detail = await self._part_table_detail(part)
            part_version = part_version_id(part)
            detail_version = (
                (detail.get("versionMetadata") or {}).get("versionId") if detail else None
            )
            if part_version and detail_version and part_version != detail_version:
                if attempt == 1:
                    raise ConflictError(
                        "Entry changed while the document part was being read",
                        details={
                            "entry_id": entry_id,
                            "part_id": part_id,
                            "part_version": part_version,
                            "table_version": detail_version,
                        },
                    )
                continue
            version = part_version or detail_version
            if not version:
                version = entry_version(await self.api.request("GET", f"/entry/{entry_id}"))
            return self._part_reply(entry_id, version, part, detail)
        raise AssertionError("unreachable")

    async def _entry_part(self, entry_id: str, part_id: str) -> dict[str, Any]:
        """One part of an entry, read with the `id.anyOf` filter."""
        matches = [
            item
            async for item in self.api.paginate(
                f"/entry/{entry_id}/parts/items",
                params={"id.anyOf": part_id, "pageSize": 100},
            )
            if item.get("id") == part_id
        ]
        if not matches:
            raise NotFoundError("Document part was not found", details={"part_id": part_id})
        if len(matches) > 1:
            raise UpstreamError(
                "Benchling returned a duplicate document part",
                details={"entry_id": entry_id, "part_id": part_id},
            )
        return matches[0]

    async def _get_part_from_snapshot(self, reference: str, part_id: str) -> dict[str, Any]:
        snapshot = await self.get_entry(reference, full=True)
        for part in snapshot["parts"]:
            if part.get("id") == part_id:
                return self._part_reply(
                    snapshot["resolved"]["entry_id"],
                    snapshot["version_id"],
                    part,
                    snapshot["tables"].get(part_id),
                )
        raise NotFoundError("Document part was not found", details={"part_id": part_id})

    @staticmethod
    def _part_reply(
        entry_id: str, version: str, part: dict[str, Any], detail: dict[str, Any] | None
    ) -> dict[str, Any]:
        reply: dict[str, Any] = {
            "entry_id": entry_id,
            "version_id": version,
            "part": part,
            "detail": detail,
        }
        if is_text_box(part):
            reply["text_box"] = text_box_detail(part)
        return reply

    async def _part_table_detail(self, part: dict[str, Any]) -> dict[str, Any] | None:
        """Table detail and every row for a table part this client reads; otherwise None."""
        part_id = part.get("id")
        if not isinstance(part_id, str) or not part_id:
            return None
        if is_unstructured_table(part):
            return await self.get_table(part_id)
        if is_results_table(part):
            return await self.get_results_table_part(part_id)
        if is_lookup_table(part):
            return await self.get_lookup_table_part(part_id)
        return None

    async def get_table(self, part_id: str) -> dict[str, Any]:
        """Read an unstructured table: part metadata, then every row page.

        Rows come from `/rows/items` at pageSize 100. The returned version is the
        entry version (`versionMetadata.versionId`), which later writes must send.
        """
        return await self._read_part_table(
            part_id,
            collection="document-part-unstructured-table",
            fallback_typename="DocumentPartUnstructuredTable",
            require_text=True,
        )

    async def get_results_table_part(self, part_id: str) -> dict[str, Any]:
        """Read a results table's typed columns and every row page."""
        return await self._read_part_table(
            part_id,
            collection="document-part-results-table",
            fallback_typename="DocumentPartResultsTable",
            require_text=False,
        )

    async def get_lookup_table_part(self, part_id: str) -> dict[str, Any]:
        """Read a lookup table's columns and every row page. Lookup rows are read-only."""
        return await self._read_part_table(
            part_id,
            collection="document-part-lookup-table",
            fallback_typename="DocumentPartLookupTable",
            require_text=False,
        )

    async def list_results_table_rows(
        self,
        part_id: object,
        *,
        entry: str | None = None,
        limit: object = listing.LIST_LIMIT,
        cursor: object = None,
    ) -> dict[str, Any]:
        """Return one page of rows from a V3 results table.

        ``part_id`` is a ``v3_document_part`` id (``docpart_…``). A
        ``v2_structured_table`` id (``strtbl_…``) from ``results tables`` is mapped
        to its document part on ``entry`` with ``document_part_for_listed_table``;
        without ``entry``, or when no single part matches, it is a validation error.
        The part is read first, and its published rows URL must be
        ``/document-part-results-table/{id}/rows/items``. That endpoint is paged
        with ``pageSize`` (1 to 100, default 50) and ``nextToken``. A short
        upstream page is followed until ``limit`` rows are collected or the
        list ends. Cells use the same shape as an entry snapshot.
        """
        page_size = listing.check_limit(limit)
        table_id = _require_part_id(part_id)
        if cursor is not None and (not isinstance(cursor, str) or not cursor):
            raise ValidationError("cursor must be a non-empty string")
        listed: dict[str, Any] = {}
        if table_id.startswith(V2_STRUCTURED_TABLE_PREFIX):
            entry_id, part = await self._results_part_for_listed_table(table_id, entry)
            listed = {
                "table_id": table_id,
                "entry_id": entry_id,
                "id_namespace": V3_DOCUMENT_PART_NAMESPACE,
                "listed_id_namespace": V2_STRUCTURED_TABLE_NAMESPACE,
            }
            table_id = part
        detail = await self.api.request("GET", f"/document-part-results-table/{table_id}")
        version = entry_version(detail)
        path = rows_items_path(
            self.api.environment.base_url,
            table_id,
            detail.get("rows"),
            collection="document-part-results-table",
        )
        row_count = detail.get("rowCount")
        if isinstance(row_count, bool) or not isinstance(row_count, int):
            raise UpstreamError(
                "Table response contained an invalid row count",
                details={"part_id": table_id},
            )
        columns = detail.get("columnDefinitions") or []
        if not isinstance(columns, list) or any(not isinstance(column, dict) for column in columns):
            raise UpstreamError(
                "Results table response contained invalid columns",
                details={"part_id": table_id},
            )
        params: dict[str, Any] = {}
        if isinstance(cursor, str):
            params["nextToken"] = cursor
        raw_rows, token = await listing._one_page(
            self.api, path, params, version=3, item_key="items", limit=page_size
        )
        schema = detail.get("resultSchema")
        schema_id = schema.get("id") if isinstance(schema, dict) else None
        return {
            "id": detail.get("id", table_id),
            **listed,
            "version_id": version,
            "name": detail.get("name"),
            "column_definitions": columns,
            "result_schema_id": schema_id if isinstance(schema_id, str) and schema_id else None,
            "row_count": row_count,
            "submission_status": detail.get("submissionStatus"),
            "items": [
                self._normalize_results_table_row(row, len(columns), part_id=table_id)
                for row in raw_rows
            ],
            "next_token": token,
        }

    async def _results_part_for_listed_table(
        self, table_id: str, entry: str | None
    ) -> tuple[str, str]:
        """Map a ``strtbl_`` id on ``entry`` to its results-table ``docpart_`` id.

        Returns the resolved entry id and the document-part id. Raises a
        validation error naming both namespaces when ``entry`` is missing, the
        table is not on the entry, or no single document part matches.
        """
        details: dict[str, Any] = {
            "part_id": table_id,
            "id_namespace": V2_STRUCTURED_TABLE_NAMESPACE,
            "expected_id_namespace": V3_DOCUMENT_PART_NAMESPACE,
        }
        if entry is None or not entry.strip():
            raise ValidationError(
                f"{table_id} is a {V2_STRUCTURED_TABLE_NAMESPACE} id from `results tables`. "
                "Pass --entry for that entry to map it, or pass a "
                f"{V3_DOCUMENT_PART_NAMESPACE} id (docpart_…) from `entry inspect`.",
                details=details,
            )
        entry_id = str((await self.resolve_entry(entry))["entry_id"])
        details["entry_id"] = entry_id
        try:
            note = await self.get_result_table(entry_id, table_id)
        except NotFoundError as exc:
            raise ValidationError(
                f"{table_id} is a {V2_STRUCTURED_TABLE_NAMESPACE} id and was not found on "
                "this entry. `results tables` lists those ids.",
                details=details,
            ) from exc
        part = await self.document_part_for_listed_table(entry_id, note, unstructured=False)
        if part is None:
            raise ValidationError(
                f"{table_id} could not be mapped to a single {V3_DOCUMENT_PART_NAMESPACE} id "
                "on this entry. Pass the results table's docpart_ id from `entry inspect`.",
                details=details,
            )
        return entry_id, part

    @staticmethod
    def _normalize_results_table_row(
        row: Any, column_count: int, *, part_id: str, position: int | None = None
    ) -> dict[str, Any]:
        normalized = normalize_table_row(
            row, require_text=False, part_id=part_id, position=position
        )
        if len(normalized["cells"]) != column_count:
            raise UpstreamError(
                "Table response contained invalid rows",
                details={
                    "part_id": part_id,
                    "row_index": normalized.get("rowIndex", position),
                    "cells": len(normalized["cells"]),
                    "columns": column_count,
                },
            )
        return normalized

    async def _read_part_table(
        self,
        part_id: str,
        *,
        collection: str,
        fallback_typename: str,
        require_text: bool,
    ) -> dict[str, Any]:
        """GET a table part, then page its rows URL. A bad shape or count is an error."""
        detail = await self.api.request("GET", f"/{collection}/{part_id}")
        version = entry_version(detail)
        path = rows_items_path(
            self.api.environment.base_url, part_id, detail.get("rows"), collection=collection
        )
        columns: list[dict[str, Any]] | None = None
        if not require_text:
            raw_columns = detail.get("columnDefinitions") or []
            if not isinstance(raw_columns, list) or any(
                not isinstance(column, dict) for column in raw_columns
            ):
                raise UpstreamError(
                    "Table response contained invalid columns",
                    details={"part_id": part_id, "type": fallback_typename},
                )
            columns = raw_columns
        rows: list[dict[str, Any]] = []
        async for item in self.api.paginate(path, params={"pageSize": TABLE_ROW_PAGE_SIZE}):
            position = len(rows)
            rows.append(
                self._normalize_results_table_row(
                    item, len(columns), part_id=part_id, position=position
                )
                if columns is not None
                else normalize_table_row(
                    item, require_text=True, part_id=part_id, position=position
                )
            )
        row_count = detail.get("rowCount")
        if isinstance(row_count, bool) or not isinstance(row_count, int):
            raise UpstreamError(
                "Table response contained an invalid row count",
                details={"part_id": part_id},
            )
        if row_count != len(rows):
            raise UpstreamError(
                "Table row count did not match the rows that were returned",
                details={"part_id": part_id, "row_count": row_count, "returned": len(rows)},
            )
        return {
            "id": detail.get("id", part_id),
            "versionMetadata": {"versionId": version},
            "rowCount": row_count,
            "content": table_content_from_detail(detail, rows, fallback=fallback_typename),
            "pagination": {"rows": len(rows), "complete": True},
        }

    async def get_table_for_entry(self, reference: str, part_id: str) -> dict[str, Any]:
        """Resolve and verify that an unstructured table part belongs to one entry."""
        resolved = await self.resolve_entry(reference)
        matches = [
            item
            async for item in self.api.paginate(
                f"/entry/{resolved['entry_id']}/parts/items",
                params={"id.anyOf": part_id, "pageSize": 100},
            )
            if item.get("id") == part_id
        ]
        if not matches:
            raise NotFoundError(
                "Table part does not belong to the selected entry",
                details={"entry_id": resolved["entry_id"], "part_id": part_id},
            )
        if len(matches) > 1:
            raise UpstreamError(
                "Benchling returned a duplicate document part",
                details={"entry_id": resolved["entry_id"], "part_id": part_id},
            )
        part = matches[0]
        if not is_unstructured_table(part):
            raise UnsupportedError(
                "Only unstructured table document parts can be edited",
                details={"part_id": part_id, "type": part_type(part)},
            )
        table = await self.get_table(part_id)
        part_version = part.get("versionId") or (part.get("versionMetadata") or {}).get("versionId")
        table_version = (table.get("versionMetadata") or {}).get("versionId")
        if part_version and table_version and part_version != table_version:
            raise ConflictError(
                "Table changed while its target entry was being verified",
                details={
                    "part_version": part_version,
                    "table_version": table_version,
                    "part_id": part_id,
                },
            )
        return {"resolved": resolved, "part": part, "table": table}

    async def get_schema(self, schema_id: str, *, kind: object = None) -> dict[str, Any]:
        """Return a schema and its field definitions.

        The collection is chosen from the id prefix: ``assaysch_`` is a result
        schema, ``runsch_`` is a run schema, ``es_`` is an entity schema, and
        any other id is an entry schema. Dropdown fields include their options.
        """
        collections = {
            "entry": "entry-schema",
            "entity": "entity-schema",
            "result": "result-schema",
            "run": "run-schema",
        }
        if kind is not None and kind not in collections:
            raise ValidationError("Schema kind must be entry, entity, result, or run")
        collection = (
            collections[str(kind)] if kind is not None else listing.schema_collection(schema_id)
        )
        base = f"/{collection}/{quote(schema_id, safe='')}"
        schema = await self.api.request("GET", base)
        fields = [
            item
            async for item in self.api.paginate(
                f"{base}/field-definitions/items", params={"pageSize": 100}
            )
        ]
        return {"schema": schema, "field_definitions": await self._with_dropdowns(fields)}

    async def find_schema(self, name: object, *, kind: object = None) -> dict[str, Any]:
        """Find one exact schema name and return its detailed, typed field definitions."""
        if not isinstance(name, str) or not name.strip():
            raise ValidationError("name must be a non-empty string")
        schema_name = name.strip()
        if "," in schema_name:
            raise ValidationError("name must not contain a comma")
        if kind is not None and (not isinstance(kind, str) or kind not in listing.SCHEMA_KINDS):
            raise ValidationError(
                "Schema kind must be entry, entity, result, or run",
                details={"supported": list(listing.SCHEMA_KINDS)},
            )
        matches: list[dict[str, Any]] = []
        cursor: str | None = None
        while True:
            page = await self.list_schemas(
                kind=kind, name=schema_name, limit=listing.LIST_LIMIT_MAX, cursor=cursor
            )
            matches.extend(page["items"])
            cursor = page["next_token"]
            if cursor is None:
                break
        if not matches:
            raise NotFoundError(
                f'No schema named "{schema_name}" was found',
                details={"name": schema_name, "kind": kind},
            )
        candidates = [{"id": item["id"], "kind": item["kind"]} for item in matches]
        if len(candidates) > 1:
            labels = ", ".join(f"{item['id']} ({item['kind']})" for item in candidates)
            raise ValidationError(
                f'Schema name "{schema_name}" is ambiguous; candidates: {labels}',
                details={"candidates": candidates},
            )
        found = matches[0]
        detail = await self.get_schema(found["id"], kind=found["kind"])
        detail["kind"] = found["kind"]
        detail["field_definitions"] = [
            {
                **field,
                "type": field_definition_type(field),
                "is_required": bool(field.get("isRequired")),
                "dropdown_options": (field.get("dropdown") or {}).get("options", []),
            }
            for field in detail["field_definitions"]
        ]
        return detail

    async def get_container(self, container_id: str) -> dict[str, Any]:
        """Return one V2 inventory container by ID."""
        container_id = self._container_reference(container_id, "container_id")
        payload = await self.api.request(
            "GET", f"/containers/{quote(container_id, safe='')}", version=2
        )
        return self._v2_object(payload, "container")

    async def get_container_schema(self, schema_id: str) -> dict[str, Any]:
        """Return one V2 container schema and its embedded field definitions."""
        schema_id = self._container_reference(schema_id, "schema_id")
        payload = await self.api.request(
            "GET", f"/container-schemas/{quote(schema_id, safe='')}", version=2
        )
        return self._v2_object(payload, "container schema")

    async def find_containers(
        self,
        *,
        barcode: object = None,
        name: object = None,
        limit: object = listing.LIST_LIMIT,
        cursor: object = None,
    ) -> dict[str, Any]:
        """Find V2 containers by exact barcode or exact name.

        A name or one barcode returns one page. Several barcodes return a summary of
        found, invalid, missing and empty containers; see _find_containers_by_barcodes.
        """
        if (barcode is None) == (name is None):
            raise ValidationError("Specify exactly one of barcode or name")
        page_size = listing.check_limit(limit)
        params: dict[str, Any] = {"pageSize": page_size}
        if barcode is not None:
            barcodes = self._container_barcodes(barcode)
            if len(barcodes) > 1:
                if cursor is not None:
                    raise ValidationError(
                        "cursor pages one barcode or a name; several barcodes are read in full"
                    )
                return await self._find_containers_by_barcodes(barcodes)
            params["barcodes"] = barcodes[0]
        else:
            params["name"] = self._container_reference(name, "name")
        if cursor is not None:
            if not isinstance(cursor, str) or not cursor:
                raise ValidationError("cursor must be a non-empty string")
            params["nextToken"] = cursor
        try:
            payload = await self.api.request("GET", "/containers", version=2, params=params)
        except ValidationError as exc:
            _lift_invalid_barcodes(exc)
            raise
        containers = payload.get("containers")
        if not isinstance(containers, list) or any(
            not isinstance(item, dict) for item in containers
        ):
            raise UpstreamError("Benchling returned an unexpected container list")
        next_token = payload.get("nextToken")
        if next_token is not None and not isinstance(next_token, str):
            raise UpstreamError("Benchling returned an invalid container pagination token")
        return {"items": containers, "next_token": next_token or None}

    def _container_barcodes(self, value: object) -> list[str]:
        """Barcodes in the caller's order without duplicates; one value per barcode."""
        values = [value] if isinstance(value, str) else value
        if not isinstance(values, (list, tuple)) or not values:
            raise ValidationError("barcode must be a non-empty string or list of strings")
        barcodes: dict[str, None] = {}
        for item in values:
            text = self._container_reference(item, "barcode")
            if "," in text:
                raise ValidationError(
                    "barcode must identify one container; repeat --barcode for each barcode"
                )
            barcodes[text] = None
        return list(barcodes)

    async def _find_containers_by_barcodes(self, barcodes: list[str]) -> dict[str, Any]:
        """Look up barcodes 100 per V2 GET /containers request, following nextToken.

        A 400 naming invalidBarcodes records them and repeats that chunk once without them.
        """
        matches: dict[str, list[dict[str, Any]]] = {}
        invalid: set[str] = set()
        for offset in range(0, len(barcodes), CONTAINER_BARCODE_CHUNK):
            chunk = barcodes[offset : offset + CONTAINER_BARCODE_CHUNK]
            try:
                containers = await self._containers_with_barcodes(chunk)
            except ValidationError as exc:
                rejected = set(exc.details.get("invalid_barcodes") or ())
                rest = [item for item in chunk if item not in rejected]
                if len(rest) == len(chunk):
                    raise
                invalid.update(item for item in chunk if item in rejected)
                containers = await self._containers_with_barcodes(rest) if rest else []
            for container in containers:
                matches.setdefault(str(container.get("barcode")), []).append(container)
        found: list[dict[str, Any]] = []
        missing: list[str] = []
        empty: list[str] = []
        for item in barcodes:
            if item in invalid:
                continue
            if item not in matches:
                missing.append(item)
                continue
            for container in matches[item]:
                summary = _container_summary(item, container)
                found.append(summary)
                if not summary["contents"] and item not in empty:
                    empty.append(item)
        return {
            "requested": len(barcodes),
            "found": found,
            "invalid_barcodes": [item for item in barcodes if item in invalid],
            "missing_barcodes": missing,
            "empty_containers": empty,
        }

    async def _containers_with_barcodes(self, barcodes: list[str]) -> list[dict[str, Any]]:
        params = {"pageSize": CONTAINER_BARCODE_CHUNK, "barcodes": ",".join(barcodes)}
        try:
            return [
                item
                async for item in self.api.paginate(
                    "/containers", version=2, params=params, item_key="containers"
                )
            ]
        except ValidationError as exc:
            _lift_invalid_barcodes(exc)
            raise

    async def list_container_schemas(
        self, *, limit: object = listing.LIST_LIMIT, cursor: object = None
    ) -> dict[str, Any]:
        """Return one page of V2 container schemas, including embedded field definitions."""
        page_size = listing.check_limit(limit)
        params: dict[str, Any] = {"pageSize": page_size}
        if cursor is not None:
            if not isinstance(cursor, str) or not cursor:
                raise ValidationError("cursor must be a non-empty string")
            params["nextToken"] = cursor
        payload = await self.api.request("GET", "/container-schemas", version=2, params=params)
        schemas = payload.get("containerSchemas")
        if not isinstance(schemas, list) or any(not isinstance(item, dict) for item in schemas):
            raise UpstreamError("Benchling returned an unexpected container schema list")
        next_token = payload.get("nextToken")
        if next_token is not None and not isinstance(next_token, str):
            raise UpstreamError("Benchling returned an invalid container schema pagination token")
        return {"items": schemas, "next_token": next_token or None}

    @staticmethod
    def _container_reference(value: object, label: str) -> str:
        """Require a safe, non-empty inventory reference without echoing user input."""
        if not isinstance(value, str) or not value.strip():
            raise ValidationError(f"{label} must be a non-empty string")
        result = value.strip()
        if any(ord(char) < 32 for char in result):
            raise ValidationError(f"{label} must not contain control characters")
        return result

    async def _with_dropdowns(self, fields: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Copy field definitions, attaching options for each dropdown field."""
        cache: dict[str, dict[str, Any]] = {}
        enriched: list[dict[str, Any]] = []
        for field in fields:
            if field_definition_type(field) != "DROPDOWN":
                enriched.append(field)
                continue
            dropdown_id = listing.dropdown_id_of(field)
            if dropdown_id is None:
                enriched.append(field)
                continue
            if dropdown_id not in cache:
                cache[dropdown_id] = await listing.load_dropdown(self.api, dropdown_id)
            enriched.append({**field, "dropdown": cache[dropdown_id]})
        return enriched

    async def list_entries(
        self,
        *,
        project: object = None,
        folder: object = None,
        schema_id: object = None,
        name: object = None,
        modified_after: object = None,
        modified_before: object = None,
        creator: object = None,
        author: object = None,
        limit: object = listing.LIST_LIMIT,
        cursor: object = None,
    ) -> dict[str, Any]:
        """Return one page of entries matching the server-side filters."""
        return await listing.list_entries(
            self.api,
            project=project,
            folder=folder,
            schema_id=schema_id,
            name=name,
            modified_after=modified_after,
            modified_before=modified_before,
            creator=creator,
            author=author,
            limit=limit,
            cursor=cursor,
        )

    async def list_schemas(
        self,
        *,
        kind: object = None,
        name: object = None,
        limit: object = listing.LIST_LIMIT,
        cursor: object = None,
    ) -> dict[str, Any]:
        """Return one page of entry, result, and run schemas."""
        return await listing.list_schemas(
            self.api, kind=kind, name=name, limit=limit, cursor=cursor
        )

    async def get_entry_fields(self, reference: str) -> dict[str, Any]:
        """Return an entry's custom fields keyed by raw and system name, with schema, project and result tables."""
        resolved = await self.resolve_entry(reference)
        entry = self._v2_object(
            await self.api.request("GET", f"/entries/{resolved['entry_id']}", version=2), "entry"
        )
        schema_ref = entry.get("schema")
        schema_detail: dict[str, Any] | None = None
        definitions: list[dict[str, Any]] = []
        if isinstance(schema_ref, dict) and schema_ref.get("id"):
            schema_detail = await self.get_schema(str(schema_ref["id"]))
            definitions = schema_detail["field_definitions"]
        raw_fields = entry.get("fields") or {}
        fields_by_system_name: dict[str, Any] = {}
        for definition in definitions:
            system_name = definition.get("systemName")
            display_name = definition.get("name")
            if not system_name:
                continue
            if display_name in raw_fields:
                value = raw_fields[display_name]
            elif system_name in raw_fields:
                value = raw_fields[system_name]
            else:
                value = None
            fields_by_system_name[str(system_name)] = value
        return {
            "entry_id": resolved["entry_id"],
            "schema": schema_ref,
            "schema_detail": schema_detail,
            "field_definitions": definitions,
            "fields": raw_fields,
            "fields_by_system_name": fields_by_system_name,
            "modified_at": entry.get("modifiedAt"),
            "project_id": await self._entry_project_id(entry),
            "result_tables": self._result_tables_from_entry(entry),
        }

    def _v2_object(self, payload: dict[str, Any], key: str) -> dict[str, Any]:
        obj = payload.get(key, payload)
        if not isinstance(obj, dict) or not obj.get("id"):
            raise UpstreamError(
                "V2 response did not contain the expected object", details={"object": key}
            )
        return obj

    async def _entry_project_id(self, entry: dict[str, Any]) -> str | None:
        project = entry.get("project") or {}
        if project.get("id"):
            return str(project["id"])
        if entry.get("folderId"):
            folder = self._v2_object(
                await self.api.request("GET", f"/folders/{entry['folderId']}", version=2), "folder"
            )
            return folder.get("projectId")
        return None

    def _result_tables_from_entry(self, entry: dict[str, Any]) -> list[dict[str, Any]]:
        tables = []
        seen: set[str] = set()
        for day in entry.get("days") or []:
            for note in day.get("notes") or []:
                if note.get("type") != "results_table":
                    continue
                table_id = note.get("apiId")
                if not isinstance(table_id, str) or not table_id.startswith("strtbl_"):
                    raise UpstreamError("Result table has no valid API identifier")
                if table_id in seen:
                    raise UpstreamError("Benchling returned a duplicate result-table identifier")
                seen.add(table_id)
                tables.append(
                    {
                        "entry_id": entry["id"],
                        "table_id": table_id,
                        "name": note.get("name"),
                        "schema_id": note.get("assayResultSchemaId"),
                        "columns": note.get("columns") or [],
                    }
                )
        return tables

    async def list_result_tables(
        self, reference: str, *, name_prefix: str | None = None
    ) -> dict[str, Any]:
        """List the V2 Results tables embedded in an entry, with its project ID.

        ``name_prefix`` keeps tables whose name starts with it (exact,
        case-sensitive). Row counts come from ``capacity.add_table_counts``.
        """
        resolved = await self.resolve_entry(reference)
        entry = self._v2_object(
            await self.api.request("GET", f"/entries/{resolved['entry_id']}", version=2), "entry"
        )
        items = self._result_tables_from_entry(entry)
        if name_prefix is not None:
            if not isinstance(name_prefix, str) or not name_prefix:
                raise ValidationError("name_prefix must be a non-empty string")
            items = [
                item
                for item in items
                if isinstance(item.get("name"), str) and item["name"].startswith(name_prefix)
            ]
        return {
            "resolved": resolved,
            "project_id": await self._entry_project_id(entry),
            "items": items,
            "complete": True,
            "id_namespace": "v2_structured_table",
        }

    async def get_result_table(self, entry_id: str, table_id: str) -> dict[str, Any]:
        """Find an explicit V2 Results table within one entry."""
        entry = self._v2_object(
            await self.api.request("GET", f"/entries/{entry_id}", version=2), "entry"
        )
        matches: list[dict[str, Any]] = []
        for day in entry.get("days") or []:
            for note in day.get("notes") or []:
                if note.get("type") == "results_table" and note.get("apiId") == table_id:
                    matches.append(note)
        if not matches:
            raise NotFoundError(
                "Result table was not found on the selected entry",
                details={"entry_id": entry_id, "table_id": table_id},
            )
        if len(matches) > 1:
            raise UpstreamError(
                "Benchling returned the same result-table ID more than once",
                details={"entry_id": entry_id, "table_id": table_id},
            )
        note = matches[0]
        return {
            "entry_id": entry_id,
            "entry_web_url": listing.entry_web_url(entry),
            "table_id": table_id,
            "name": note.get("name"),
            "schema_id": note.get("assayResultSchemaId"),
            "columns": note.get("columns") or [],
        }

    async def document_part_for_listed_table(
        self,
        entry_id: str,
        note: dict[str, Any],
        *,
        unstructured: bool,
        parts: list[dict[str, Any]] | None = None,
    ) -> str | None:
        """Return the V3 document-part id for a V2 ``strtbl_`` note, when one part matches.

        A handle (``apiId``, ``tableHandleId``, or the part id) wins. A results table
        otherwise matches its result schema, and its name when several parts share
        that schema. The only results part is used when the note has no schema.
        An unstructured table matches only by handle. ``strtbl_`` is not itself a
        document-part id. ``parts`` are the entry's V3 parts when the caller has
        already read them; otherwise they are read here.
        """
        table_id = note.get("table_id")
        if not isinstance(table_id, str) or not table_id:
            return None
        if parts is None:
            parts = [
                item
                async for item in self.api.paginate(
                    f"/entry/{entry_id}/parts/items", params={"pageSize": 100}
                )
            ]
        predicate = is_unstructured_table if unstructured else is_results_table
        typed = [part for part in parts if predicate(part) and isinstance(part.get("id"), str)]
        direct = [part for part in typed if table_id in table_handle_ids(part)]
        if len(direct) == 1:
            return str(direct[0]["id"])
        if unstructured or len(direct) > 1:
            return None
        schema_id = note.get("schema_id")
        name = note.get("name") if isinstance(note.get("name"), str) and note.get("name") else None
        schema_matches = [
            part
            for part in typed
            if isinstance(schema_id, str) and schema_id and part_result_schema_id(part) == schema_id
        ]
        if len(schema_matches) == 1:
            return str(schema_matches[0]["id"])
        if name:
            named = [part for part in schema_matches if part_display_name(part) == name]
            if len(named) == 1:
                return str(named[0]["id"])
        if (
            len(typed) == 1
            and not schema_matches
            and (
                not isinstance(schema_id, str)
                or not schema_id
                or part_result_schema_id(typed[0]) in {None, schema_id}
            )
        ):
            return str(typed[0]["id"])
        return None

    async def get_project(self, project_id: str) -> dict[str, Any]:
        """Fetch a V2 project; raises UpstreamError if the response lacks an object ID."""
        return self._v2_object(
            await self.api.request("GET", f"/projects/{project_id}", version=2), "project"
        )

    async def get_task(self, task_id: str) -> dict[str, Any]:
        """Fetch a V2 task; raises UpstreamError if the response has no string status."""
        payload = await self.api.request("GET", f"/tasks/{task_id}", version=2)
        task = payload.get("task", payload)
        # Task responses may be a flat status object without an id property.
        if not isinstance(task, dict) or not isinstance(task.get("status"), str):
            raise UpstreamError("V2 task response did not contain a status")
        return task

    async def list_results(
        self,
        *,
        entry: str | None = None,
        schema_id: str | None = None,
        page_size: int = 100,
        cursor: str | None = None,
        v3_ids: list[str] | None = None,
    ) -> dict[str, Any]:
        """Return one page of results, optionally filtered by entry, schema, and V3 ids.

        page_size must be 1-100 (ValidationError otherwise); next_token is None on the last page.
        ``v3_ids`` (``result_…``) is sent as ``id.anyOf``.
        """
        if page_size < 1 or page_size > 100:
            raise ValidationError("page_size must be between 1 and 100")
        params: dict[str, Any] = {"pageSize": page_size}
        if cursor:
            params["nextToken"] = cursor
        if entry:
            params["entry.eq"] = (await self.resolve_entry(entry))["entry_id"]
        if schema_id:
            params["schema.eq"] = schema_id
        if v3_ids:
            params["id.anyOf"] = ",".join(v3_ids)
        payload = await self.api.request("GET", "/result/items", params=params)
        return {
            "items": payload.get("items", []),
            "next_token": payload.get("nextToken") or None,
        }

    async def list_all_results(
        self,
        *,
        entry: str | None = None,
        schema_id: str | None = None,
    ) -> dict[str, Any]:
        """Return every assay-result page for these filters.

        Pages are followed internally at pageSize 100 until nextToken is absent.
        ``entry`` is resolved once. ``schema_id`` is sent as ``schema.eq`` when given.
        """
        params: dict[str, Any] = {"pageSize": listing.LIST_LIMIT_MAX}
        resolved: str | None = None
        if entry is not None:
            resolved = str((await self.resolve_entry(entry))["entry_id"])
            params["entry.eq"] = resolved
        if schema_id is not None:
            params["schema.eq"] = schema_id
        items = [item async for item in self.api.paginate("/result/items", params=params)]
        return {"items": items, "entry_id": resolved, "schema_id": schema_id}

    async def get_assay_results(self, result_ids: object) -> dict[str, Any]:
        """Fetch V2 assay results by V2 UUID or V3 ``result_…`` id.

        V2 ids go through bulk-get, 200 per request. A V3 id is never sent there: it is
        read with V3 GET /result/{id} and matched to its V2 record by ``v3Id``. IDs that
        cannot be read are listed under ``not_found``, in the order given. Pending results
        are included: they are records without an entry.
        """
        requested = self._bulk_lookup_ids(result_ids, "result_ids") or []
        unique = list(dict.fromkeys(requested))
        v3_ids = [item for item in unique if result_id_namespace(item) == V3_RESULT_NAMESPACE]
        found = await self._bulk_get_v2_results([item for item in unique if item not in v3_ids])
        if v3_ids:
            v3_records: list[dict[str, Any]] = []
            for v3_id in v3_ids:
                try:
                    v3_records.append(
                        await self.api.request("GET", f"/result/{quote(v3_id, safe='')}")
                    )
                except NotFoundError:
                    continue
            found.update(await self._v2_results_for_v3(v3_records))
        items: list[dict[str, Any]] = []
        seen: set[str] = set()
        for result_id in unique:
            record = found.get(result_id)
            if record is not None and record["id"] not in seen:
                seen.add(record["id"])
                items.append(record)
        return {
            "items": items,
            "not_found": [result_id for result_id in unique if result_id not in found],
        }

    async def _bulk_get_v2_results(self, result_ids: list[str]) -> dict[str, dict[str, Any]]:
        """V2 bulk-get, 200 ids per request, keyed by V2 id. A 400 names the id namespace."""
        found: dict[str, dict[str, Any]] = {}
        for offset in range(0, len(result_ids), ASSAY_RESULT_BULK_GET_LIMIT):
            batch = result_ids[offset : offset + ASSAY_RESULT_BULK_GET_LIMIT]
            try:
                payload = await self.api.request(
                    "GET",
                    "/assay-results:bulk-get",
                    version=2,
                    params={"assayResultIds": ",".join(batch)},
                )
            except ValidationError as exc:
                namespaces = sorted({result_id_namespace(item) for item in batch})
                raise ValidationError(
                    "Benchling rejected one or more ids as V2 assay result ids. Pass V2 assay "
                    "result UUIDs or V3 result ids (result_…); each is read in its own namespace.",
                    details={
                        **exc.details,
                        "id_namespace": namespaces[0] if len(namespaces) == 1 else namespaces,
                        "expected_id_namespace": V2_ASSAY_RESULT_NAMESPACE,
                        "lookup": V2_RESULT_LOOKUP,
                        "accepted_id_namespaces": list(RESULT_ID_NAMESPACES),
                        "result_ids": batch[:20],
                    },
                ) from exc
            records = payload.get("assayResults")
            if not isinstance(records, list) or any(not isinstance(item, dict) for item in records):
                raise UpstreamError("Benchling returned an unexpected bulk result response")
            for item in records:
                if isinstance(item.get("id"), str):
                    found[item["id"]] = item
        return found

    async def _v2_results_for_v3(self, records: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
        """Map V3 result ids to their V2 assay results, archived ones included.

        V3 results do not carry the V2 UUID, and V2 has no lookup by V3 id. Records of a
        schema are grouped into runs created within two seconds of each other. For each
        run, V2 GET /assay-results is read in creation order from a second before to a
        second after it and matched on ``v3Id``, at most V2_ID_LOOKUP_MAX_PAGES pages plus
        one per hundred ids wanted.
        A record without a schema or createdAt, or not matched, is left out.
        """
        by_schema: dict[str, list[tuple[datetime, str]]] = {}
        for record in records:
            v3_id, schema_id = record.get("id"), _result_schema_id(record)
            created = _result_created_at(record)
            if isinstance(v3_id, str) and schema_id is not None and created is not None:
                by_schema.setdefault(schema_id, []).append((created, v3_id))
        windows: list[tuple[str, datetime, datetime, set[str]]] = []
        for schema_id, stamped in by_schema.items():
            for created, v3_id in sorted(stamped):
                last = windows[-1] if windows else None
                if last and last[0] == schema_id and created - last[2] <= timedelta(seconds=2):
                    windows[-1] = (schema_id, last[1], created, last[3] | {v3_id})
                else:
                    windows.append((schema_id, created, created, {v3_id}))
        found: dict[str, dict[str, Any]] = {}
        for schema_id, low, high, wanted in windows:
            params: dict[str, Any] = {
                "schemaId": schema_id,
                "createdAt.gte": _rfc3339(low - timedelta(seconds=1)),
                "createdAt.lte": _rfc3339(high + timedelta(seconds=1)),
                "archiveReason": "ANY_ARCHIVED_OR_NOT_ARCHIVED",
                "sort": "createdAt:asc",
                "pageSize": 100,
            }
            for _page in range(V2_ID_LOOKUP_MAX_PAGES + len(wanted) // 100):
                if wanted <= found.keys():
                    break
                payload = await self.api.request("GET", "/assay-results", version=2, params=params)
                items = payload.get("assayResults")
                if not isinstance(items, list):
                    raise UpstreamError("Benchling returned an unexpected result list response")
                for item in items:
                    if isinstance(item, dict) and isinstance(item.get("id"), str):
                        if item.get("v3Id") in wanted:
                            found[str(item["v3Id"])] = item
                token = payload.get("nextToken")
                if not token:
                    break
                params["nextToken"] = token
        return found

    async def _v2_ids_for_v3(self, records: list[dict[str, Any]]) -> dict[str, str]:
        """V2 UUID per V3 result id where it can be read; a refused V2 read gives none."""
        try:
            matched = await self._v2_results_for_v3(records)
        except (AccessError, NotFoundError, ValidationError):
            return {}
        return {v3_id: str(record["id"]) for v3_id, record in matched.items()}

    async def get_result_by_any_id(self, result_id: str) -> dict[str, Any]:
        """Read one result by V3 ``result_…`` id or V2 assay result UUID.

        Returns the V3 record with ``v2_id`` and ``v3_id`` added; ``v2_id`` is None when
        Benchling does not return it. A V2 UUID is read with V2 bulk-get first to find its
        ``v3Id``. An id that cannot be read is a not_found naming the namespace passed and
        the lookup used.
        """
        if not isinstance(result_id, str) or not result_id.strip():
            raise ValidationError("Result ID must be a non-empty string")
        result_id = result_id.strip()
        if result_id_namespace(result_id) == V2_ASSAY_RESULT_NAMESPACE:
            found = await self._bulk_get_v2_results([result_id])
            if result_id not in found:
                raise _result_not_found(
                    result_id, expected=V2_ASSAY_RESULT_NAMESPACE, lookup=V2_RESULT_LOOKUP
                )
            v3_id = found[result_id].get("v3Id")
            if not isinstance(v3_id, str) or not v3_id:
                raise _result_not_found(
                    result_id,
                    expected=V2_ASSAY_RESULT_NAMESPACE,
                    lookup=V2_RESULT_LOOKUP,
                    reason="has no V3 result id in Benchling",
                )
            lookup = f"{V2_RESULT_LOOKUP}, then {V3_RESULT_LOOKUP}"
            record = await self._get_v3_result(v3_id, requested=result_id, lookup=lookup)
            return with_result_ids(record, result_id, record.get("id") or v3_id)
        record = await self._get_v3_result(result_id, requested=result_id, lookup=V3_RESULT_LOOKUP)
        record_id = record.get("id")
        v3_id = record_id if isinstance(record_id, str) else result_id
        v2_ids = await self._v2_ids_for_v3([{**record, "id": v3_id}])
        return with_result_ids(record, v2_ids.get(v3_id), v3_id)

    async def _get_v3_result(self, v3_id: str, *, requested: str, lookup: str) -> dict[str, Any]:
        try:
            return await self.api.request("GET", f"/result/{quote(v3_id, safe='')}")
        except NotFoundError as exc:
            error = _result_not_found(requested, expected=V3_RESULT_NAMESPACE, lookup=lookup)
            error.details["http_status"] = exc.details.get("status")
            raise error from exc

    async def list_results_by_any_id(
        self,
        *,
        entry: str | None = None,
        schema_id: str | None = None,
        result_ids: list[str] | None = None,
        page_size: int = 100,
        cursor: str | None = None,
    ) -> dict[str, Any]:
        """One page of V3 results with ``v2_id`` and ``v3_id`` on each item.

        ``result_ids`` may mix V3 ``result_…`` ids and V2 assay result UUIDs, at most 100,
        and is sent as ``id.anyOf``. V2 ids are mapped to V3 with bulk-get; a V2 id that
        cannot be mapped is listed in ``not_found`` with its namespace and lookup.
        ``v2_id`` is None when Benchling does not return it.
        """
        requested = self._bulk_lookup_ids(result_ids, "result_ids")
        v2_of: dict[str, str] = {}
        not_found: list[dict[str, Any]] = []
        v3_ids: list[str] | None = None
        if requested is not None:
            unique = list(dict.fromkeys(requested))
            if len(unique) > RESULT_ID_FILTER_LIMIT:
                raise ValidationError(
                    f"Pass at most {RESULT_ID_FILTER_LIMIT} result ids to results list"
                )
            v2_requested = [
                item for item in unique if result_id_namespace(item) != V3_RESULT_NAMESPACE
            ]
            found = await self._bulk_get_v2_results(v2_requested)
            v3_ids = []
            for item in unique:
                if item not in v2_requested:
                    v3_ids.append(item)
                    continue
                v3_id = (found.get(item) or {}).get("v3Id")
                if isinstance(v3_id, str) and v3_id:
                    v3_ids.append(v3_id)
                    v2_of[v3_id] = item
                else:
                    not_found.append(assay_result_lookup(item))
            v3_ids = list(dict.fromkeys(v3_ids))
            if not v3_ids:
                return {"items": [], "next_token": None, "not_found": not_found}
        page = await self.list_results(
            entry=entry, schema_id=schema_id, page_size=page_size, cursor=cursor, v3_ids=v3_ids
        )
        items = [item for item in page["items"] if isinstance(item, dict)]
        missing = [item for item in items if item.get("id") not in v2_of]
        if missing:
            v2_of.update(await self._v2_ids_for_v3(missing))
        page["items"] = [
            with_result_ids(item, v2_of.get(str(item.get("id"))), item.get("id")) for item in items
        ]
        if requested is not None:
            page["not_found"] = not_found
        return page

    async def list_assay_results(
        self,
        *,
        schema_id: str,
        created_after: str,
        page_size: int = 50,
        cursor: str | None = None,
    ) -> dict[str, Any]:
        """Return one page of V2 assay results for a schema created at or after a time.

        V2 ``GET /assay-results`` has no entry filter, so pending results (no entry yet)
        are found by schema and creation time. ``created_after`` is RFC 3339.
        """
        if not isinstance(schema_id, str) or not schema_id.strip():
            raise ValidationError("schema_id is required")
        if not isinstance(created_after, str) or not created_after.strip():
            raise ValidationError("created_after is required")
        try:
            datetime.fromisoformat(created_after.strip())
        except ValueError as exc:
            raise ValidationError(
                "created_after must be an RFC 3339 date or datetime, e.g. 2026-11-19T00:00:00Z"
            ) from exc
        if isinstance(page_size, bool) or page_size < 1 or page_size > 100:
            raise ValidationError("page_size must be between 1 and 100")
        params: dict[str, Any] = {
            "schemaId": schema_id.strip(),
            "createdAt.gte": created_after.strip(),
            "sort": "createdAt:asc",
            "pageSize": page_size,
        }
        if cursor:
            params["nextToken"] = cursor
        payload = await self.api.request("GET", "/assay-results", version=2, params=params)
        records = payload.get("assayResults")
        if not isinstance(records, list) or any(not isinstance(item, dict) for item in records):
            raise UpstreamError("Benchling returned an unexpected result list response")
        return {"items": records, "next_token": payload.get("nextToken") or None}

    async def get_result(self, result_id: str) -> dict[str, Any]:
        """Fetch a single result by ID."""
        return await self.api.request("GET", f"/result/{result_id}")

    async def get_blob(self, blob_id: str) -> dict[str, Any]:
        """Read blob or attachment metadata without downloading its contents."""
        _, namespace, metadata = await self._blob_metadata(blob_id)
        return {key: value for key, value in metadata.items() if key != "downloadUrl"} | {
            "id_namespace": namespace
        }

    async def _blob_metadata(self, blob_id: str) -> tuple[str, str, dict[str, Any]]:
        identifier = blob_id.strip() if isinstance(blob_id, str) else ""
        if re.fullmatch(r"attachment_[A-Za-z0-9]+", identifier):
            namespace, route, version = "v3_attachment", "/attachment/", 3
        else:
            namespace = "v3_attachment" if identifier.startswith("attachment_") else "unknown"
            try:
                parsed = UUID(identifier)
                if str(parsed) != identifier.lower():
                    raise ValueError
            except ValueError as exc:
                raise ValidationError(
                    "Expected a blob UUID or attachment_ ID",
                    details={
                        "id_namespace": namespace,
                        "expected_id_namespace": ["v2_blob_uuid", "v3_attachment"],
                    },
                ) from exc
            identifier = str(parsed)
            namespace, route, version = "v2_blob_uuid", "/blobs/", 2
        try:
            payload = await self.api.request(
                "GET", route + quote(identifier, safe=""), version=version
            )
        except NotFoundError as exc:
            exc.details.update({"id_namespace": namespace, "expected_id_namespace": namespace})
            raise
        if payload.get("id") != identifier:
            raise UpstreamError(
                "Benchling returned unexpected blob or attachment metadata",
                details={"id_namespace": namespace, "expected_id_namespace": namespace},
            )
        return identifier, namespace, payload

    async def get_linked_entity_schema_id(self, entity_id: str) -> str:
        """Read a linked entity and return its entity schema ID for result-plan validation."""
        entity_routes = {
            "ent_": ("custom-entity", "customEntity"),
            "seq_": ("dna-sequence", "dnaSequence"),
            "aaseq_": ("aa-sequence", "aaSequence"),
        }
        route = next(
            (value for prefix, value in entity_routes.items() if entity_id.startswith(prefix)),
            None,
        )
        if route is None:
            raise ValidationError(
                "Linked entity ID has an unsupported namespace",
                details={"reason": "unresolvable_link_target", "entity_id": entity_id},
            )
        collection, wrapper = route
        payload = await self.api.request("GET", f"/{collection}/{quote(entity_id, safe='')}")
        entity = payload.get(wrapper, payload)
        if not isinstance(entity, dict) or entity.get("id") != entity_id:
            raise UpstreamError(
                "Benchling returned an unexpected linked entity",
                details={"entity_id": entity_id},
            )
        schema = entity.get("typedSchema") or entity.get("schema")
        schema_id = schema.get("id") if isinstance(schema, dict) else None
        if not isinstance(schema_id, str) or not schema_id:
            raise UpstreamError(
                "Linked entity response did not include its schema",
                details={"entity_id": entity_id},
            )
        return schema_id

    async def doctor(self) -> dict[str, Any]:
        """Probe V3 and V2 entry endpoints; failures are reported per check rather than raised."""
        checks: list[dict[str, Any]] = []
        env_name = self.api.environment.name
        for name, version, path, params in (
            ("v3_entries", 3, "/entry/items", {"pageSize": 1}),
            ("v2_entries", 2, "/entries", {"pageSize": 1}),
        ):
            try:
                await self.api.request("GET", path, version=version, params=params)
                checks.append({"name": name, "ok": True})
            except Exception as exc:
                checks.append(
                    {
                        "name": name,
                        "ok": False,
                        "error": _error_text(exc),
                        "code": getattr(exc, "code", type(exc).__name__),
                        "hint": _doctor_hint(exc, env_name),
                    }
                )
        by_name = {check["name"]: check["ok"] for check in checks}
        v3_ok = by_name.get("v3_entries", False)
        v2_ok = by_name.get("v2_entries", False)
        return {
            "env": self.api.environment.name,
            "tenant_url": self.api.environment.base_url,
            "auth_method": self.api.environment.auth_method.value,
            "protected": self.api.environment.protected,
            "protection": protection_summary(self.api.environment),
            "checks": checks,
            "capabilities": {
                "document_parts": v3_ok,
                "entry_fields": v3_ok and v2_ok,
                "unstructured_table_cell_edit": v3_ok,
                "unstructured_table_row_insert": v3_ok,
                "structured_table_edit": False,
                "result_upload_pending_integration": v2_ok,
                "mcp_transport": "stdio",
            },
            "ok": all(check["ok"] for check in checks),
        }

    async def _assemble_snapshot(self, entry: dict[str, Any], version: str) -> EntrySnapshot:
        entry_id = str(entry["id"])
        parts = [
            item
            async for item in self.api.paginate(
                f"/entry/{entry_id}/parts/items", params={"pageSize": 100}
            )
        ]
        issues: list[CompletenessIssue] = []
        tables = await self._collect_part_tables(parts, issues)
        schema = await self._fetch_entry_schema(entry, issues)
        fields = await self._fetch_entry_fields(entry_id, issues)
        results, result_retrieval = await self._fetch_linked_results(entry_id, issues)

        return EntrySnapshot(
            entry=entry,
            version_id=version,
            parts=parts,
            sections=build_sections(parts),
            tables=tables,
            entry_schema=schema,
            fields=fields,
            result_tables=(fields or {}).get("result_tables", []),
            results=results,
            result_retrieval=result_retrieval,
            completeness="partial" if issues else "complete",
            issues=issues,
        )

    async def _collect_part_tables(
        self, parts: list[dict[str, Any]], issues: list[CompletenessIssue]
    ) -> dict[str, dict[str, Any]]:
        """Fetch table rows. A table this client can read fails the snapshot if its rows do not.

        Unknown part types stay in the snapshot as opaque data. A part Benchling marks
        unavailable is reported on its own, not as an unknown type.
        """
        tables: dict[str, dict[str, Any]] = {}
        for part in parts:
            part_id = part.get("id")
            kind = part_type(part).split(".")[-1]
            if is_unavailable_part(part):
                issues.append(
                    CompletenessIssue(
                        scope=f"part:{part_id or 'unknown'}",
                        reason="document_part_unavailable",
                        details={
                            "type": part_type(part),
                            "message": (
                                "Benchling did not return this part's type at the "
                                "requested API stability"
                            ),
                        },
                    )
                )
            elif kind not in KNOWN_PART_TYPES:
                issues.append(
                    CompletenessIssue(
                        scope=f"part:{part_id or 'unknown'}",
                        reason="unknown_part_type_preserved_as_opaque_data",
                        details={"type": part_type(part)},
                    )
                )
            detail = await self._part_table_detail(part)
            if detail is not None and isinstance(part_id, str):
                tables[part_id] = detail
            elif is_structured_table(part) and part_id:
                issues.append(
                    CompletenessIssue(
                        scope=f"table:{part_id}",
                        reason="structured_table_detail_not_available_through_document_api",
                    )
                )
        return tables

    async def _fetch_entry_schema(
        self, entry: dict[str, Any], issues: list[CompletenessIssue]
    ) -> dict[str, Any] | None:
        """Fetch the entry's schema, recording an issue if it is unavailable."""
        schema_ref = entry.get("typedSchema") or entry.get("schema")
        if not (isinstance(schema_ref, dict) and schema_ref.get("id")):
            return None
        try:
            return await self.get_schema(str(schema_ref["id"]))
        except Exception as exc:
            issues.append(
                CompletenessIssue(
                    scope="entry_schema",
                    reason="schema_unavailable",
                    details={"error": _error_text(exc)},
                )
            )
            return None

    async def _fetch_entry_fields(
        self, entry_id: str, issues: list[CompletenessIssue]
    ) -> dict[str, Any] | None:
        """Fetch V2 entry fields, recording an issue if they are unavailable."""
        try:
            return await self.get_entry_fields(entry_id)
        except Exception as exc:
            issues.append(
                CompletenessIssue(
                    scope="entry_fields",
                    reason="v2_fields_unavailable",
                    details={"error": _error_text(exc)},
                )
            )
            return None

    async def _fetch_linked_results(
        self, entry_id: str, issues: list[CompletenessIssue]
    ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        """Page through all results linked to the entry; returns records and retrieval metadata."""
        results: list[dict[str, Any]] = []
        result_retrieval: dict[str, Any] = {
            "consistency": "independent_non_atomic",
            "entry_filter": entry_id,
            "started_at": datetime.now(UTC).isoformat(),
            "pages": 0,
            "complete": False,
        }
        try:
            cursor: str | None = None
            while True:
                page = await self.list_results(entry=entry_id, page_size=100, cursor=cursor)
                result_retrieval["pages"] += 1
                results.extend(page["items"])
                cursor = page["next_token"]
                if not cursor:
                    break
            result_retrieval["complete"] = True
        except Exception as exc:
            result_retrieval["error"] = _error_text(exc)
            issues.append(
                CompletenessIssue(
                    scope="results",
                    reason="linked_results_unavailable",
                    details={"error": _error_text(exc)},
                )
            )
        result_retrieval["finished_at"] = datetime.now(UTC).isoformat()
        result_retrieval["records"] = len(results)
        return results, result_retrieval
