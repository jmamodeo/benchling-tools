"""Typed change specifications for the MCP changes_plan tool.

The CLI still passes the JSON object through to the planner. MCP validates the
same shapes first and returns a validation_error envelope when one does not match.
"""

from __future__ import annotations

import copy
from typing import Annotated, Any, Literal, TypeVar, cast

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    TypeAdapter,
    ValidationError,
    field_validator,
    model_validator,
)

from .errors import ValidationError as InputError

_STRICT = ConfigDict(extra="forbid", strict=True)


_T = TypeVar("_T")


def _omit(value: _T | None) -> _T:
    """Reject JSON null for a field the caller may only omit."""
    if value is None:
        raise ValueError("null is not accepted; omit the field")
    return value


class InsertedPart(BaseModel):
    """One document part to insert. Other part fields are kept."""

    model_config = ConfigDict(extra="allow", strict=True)

    contentType: str = Field(min_length=1, description="Part content type, such as text.")


class DocumentInsert(BaseModel):
    """Insert one or more document parts."""

    model_config = _STRICT

    operation: Literal["insert"] = Field(description="Insert one or more document parts.")
    position: str = Field(
        default="end_of_document",
        min_length=1,
        description=(
            "Where to insert. beginning_of_document, end_of_document, or a part ID. "
            "A part ID inserts immediately before that part. Defaults to end_of_document."
        ),
    )
    parts: list[InsertedPart] = Field(
        min_length=1,
        description="Document parts to insert. Each part needs a contentType.",
    )


class DocumentDelete(BaseModel):
    """Delete one document part or a contiguous range."""

    model_config = _STRICT

    operation: Literal["delete"] = Field(description="Delete a range of document parts.")
    from_part_id: str = Field(
        min_length=1, description="First document part ID in the range to delete."
    )
    to_part_id: str | None = Field(
        default=None,
        min_length=1,
        description="Last document part ID in the range. Omit it to delete only from_part_id.",
    )

    @field_validator("to_part_id")
    @classmethod
    def to_part_present(cls, value: str | None) -> str | None:
        """Allow the field to be omitted, and reject JSON null."""
        return _omit(value)


class DocumentMove(BaseModel):
    """Move one document part or a contiguous range."""

    model_config = _STRICT

    operation: Literal["move"] = Field(description="Move a range of document parts.")
    from_part_id: str = Field(
        min_length=1, description="First document part ID in the range to move."
    )
    to_part_id: str | None = Field(
        default=None,
        min_length=1,
        description="Last document part ID in the range. Omit it to move only from_part_id.",
    )
    position: str = Field(
        min_length=1,
        description=(
            "Where to move the range. beginning_of_document, end_of_document, or a part ID. "
            "A part ID moves the range immediately before that part."
        ),
    )

    @field_validator("to_part_id")
    @classmethod
    def to_part_present(cls, value: str | None) -> str | None:
        """Allow the field to be omitted, and reject JSON null."""
        return _omit(value)


class DocumentReplaceText(BaseModel):
    """Replace the text of one text document part."""

    model_config = _STRICT

    operation: Literal["replace_text"] = Field(
        description="Replace the text of one text document part."
    )
    part_id: str = Field(min_length=1, description="Document part ID of the text part to replace.")
    text: str = Field(description="Replacement text.")
    formats: list[Any] | None = Field(
        default=None,
        description="Text formats to store. Omit to keep the part's current formats.",
    )
    links: list[Any] | None = Field(
        default=None,
        description="Links to store. Omit to keep the part's current links.",
    )
    mentions: list[Any] | None = Field(
        default=None,
        description="Mentions to store. Omit to keep the part's current mentions.",
    )
    indentation: int | None = Field(
        default=None,
        ge=0,
        description="Indentation level, zero or greater. Omit to keep the current indentation.",
    )

    @field_validator("formats", "links", "mentions", "indentation")
    @classmethod
    def optional_present(cls, value: Any) -> Any:
        """Allow these fields to be omitted, and reject JSON null."""
        return _omit(value)


class CellUpdate(BaseModel):
    """One unstructured-table cell write."""

    model_config = _STRICT

    row: int = Field(ge=0, description="Zero-based row index.")
    column: int = Field(ge=0, description="Zero-based column index.")
    value: Any = Field(description="New cell input. It is stored as text.")


class TableSetCells(BaseModel):
    """Set the input of selected unstructured-table cells."""

    model_config = _STRICT

    operation: Literal["set_cells"] = Field(
        description="Replace the input of selected unstructured-table cells."
    )
    table_part_id: str = Field(
        min_length=1,
        pattern=r"^docpart_",
        description="Document part ID of the unstructured table. It starts with docpart_.",
    )
    updates: list[CellUpdate] = Field(
        min_length=1,
        description="Cells to write. Each item has row, column, and value.",
    )


class TableAppendRows(BaseModel):
    """Append rows to an unstructured table."""

    model_config = _STRICT

    operation: Literal["append_rows"] = Field(
        description="Append rows to the end of an unstructured table."
    )
    table_part_id: str = Field(
        min_length=1,
        pattern=r"^docpart_",
        description="Document part ID of the unstructured table. It starts with docpart_.",
    )
    rows: list[Any] = Field(
        min_length=1,
        description=(
            "Rows to append. Each row is a list of cells or an object with a cells list. "
            "The row must match the table width."
        ),
    )


class FieldValueInput(BaseModel):
    """One schema field value. The only property is value."""

    model_config = _STRICT

    value: Any = Field(
        description=(
            "New field value, or null to clear an optional field. The type must match "
            "the schema field."
        )
    )


class FieldChange(BaseModel):
    """Update fields on the entry's assigned schema."""

    model_config = _STRICT

    schema_fields: dict[str, FieldValueInput] = Field(
        min_length=1,
        description=(
            "Entry schema fields to update. Keys are V3 system names or field-definition "
            "IDs. Each value is an object with a single value property."
        ),
    )


class AssayResultRecord(BaseModel):
    """One new result record for a notebook table upload."""

    model_config = _STRICT

    schemaId: str = Field(
        min_length=1,
        description="Result schema ID. It must be the schema assigned to the table.",
    )
    projectId: str | None = Field(
        default=None,
        min_length=1,
        description="Project ID for the new result, or null to leave it unscoped.",
    )
    fields: dict[str, FieldValueInput] = Field(
        description=(
            "Result fields. Keys are V3 system names or field-definition IDs. Each value "
            "is an object with a single value property. On an attachment field, the value "
            'may be {"file": PATH} (optional blob_type RAW_FILE or VISUALIZATION, optional '
            "mime_type) to upload that local file as a blob at apply and link its blob ID."
        )
    )

    @model_validator(mode="before")
    @classmethod
    def omit_id(cls, data: Any) -> Any:
        """Result ids are assigned by the server. A caller-supplied id is rejected."""
        if isinstance(data, dict) and "id" in data:
            raise ValueError("Notebook table uploads use server-assigned result IDs; omit id")
        return data


class ResultUpload(BaseModel):
    """Upload new result records to an explicit V2 result table."""

    model_config = _STRICT

    table_id: str = Field(
        min_length=1,
        pattern=r"^strtbl_",
        description="V2 result table ID. It starts with strtbl_.",
    )
    assay_results: list[AssayResultRecord] = Field(
        min_length=1,
        description="New result records to upload to that table.",
    )


class ResultUploadBatch(BaseModel):
    """Upload records to one explicit V2 result table as part of a multi-table plan."""

    model_config = _STRICT

    table_id: str = Field(
        min_length=1,
        pattern=r"^strtbl_",
        description="V2 result table ID. It starts with strtbl_.",
    )
    assay_results: list[AssayResultRecord] = Field(
        min_length=1,
        description="New result records to upload to this table.",
    )


class MultiTableResultUpload(BaseModel):
    """Upload records to several V2 result tables in one plan."""

    model_config = _STRICT

    batches: list[ResultUploadBatch] = Field(
        min_length=1,
        description="Table-specific result batches. Each table can contain at most 500 records.",
    )


class ResultCsvUpdate(BaseModel):
    """Update existing results from a CSV file and an explicit column map."""

    model_config = _STRICT

    operation: Literal["update"] = Field(description="Update existing results from a CSV file.")
    csv_path: str = Field(
        min_length=1,
        description=(
            "Path to a CSV file on this machine. An assay-result export writes a file "
            "this planner can read."
        ),
    )
    id_column: str = Field(
        min_length=1,
        description="CSV header that holds the result id. Export writes this column as id.",
    )
    column_map: dict[str, Annotated[str, Field(min_length=1)]] = Field(
        min_length=1,
        description=(
            "CSV header to a field display name, system name, or field-definition id. "
            "The plan resolves each one to a field-definition id. In an attachment column, "
            "a cell file:PATH uploads that local file (relative to the CSV's folder)."
        ),
    )

    @field_validator("column_map")
    @classmethod
    def column_names(cls, value: dict[str, str]) -> dict[str, str]:
        """Reject a blank CSV header. The planner strips and checks the rest."""
        if any(not key.strip() for key in value):
            raise ValueError("column_map keys must be CSV column names")
        return value


class EntryCreationSpecification(BaseModel):
    """Create a notebook entry from a template in a folder."""

    model_config = _STRICT

    entry_template_id: str = Field(min_length=1, description="ID of the entry template to clone.")
    folder_id: str = Field(min_length=1, description="ID of the destination folder.")
    name: str = Field(min_length=1, description="Name of the new notebook entry.")
    schema_fields: dict[str, FieldValueInput] | None = Field(
        default=None,
        min_length=1,
        description=(
            "Optional schema values using system names or field-definition IDs. "
            "The plan resolves keys to field-definition IDs."
        ),
    )

    @field_validator("schema_fields")
    @classmethod
    def schema_fields_present(cls, value: dict[str, Any] | None) -> dict[str, Any] | None:
        """Allow schema_fields to be omitted, but not explicitly null."""
        return _omit(value)


class ContainerCreate(BaseModel):
    """Create one container after reviewing an immutable preview."""

    model_config = _STRICT

    operation: Literal["create"] = Field(description="Preview creation of a container.")
    schema_id: str = Field(min_length=1, description="Container schema ID.")
    name: str | None = Field(default=None, description="Optional container name.")
    barcode: str | None = Field(default=None, description="Optional container barcode.")
    project_id: str | None = Field(default=None, description="Optional project ID.")
    parent_storage_id: str | None = Field(
        default=None, description="Optional parent inventory or well coordinate."
    )
    fields: dict[str, Any] | None = Field(default=None, description="Container field values.")
    restricted_sample_party_ids: list[str] | None = Field(
        default=None, description="Optional restricted sample party IDs."
    )
    restriction_status: str | None = Field(
        default=None, description="Optional RESTRICTED or UNRESTRICTED status."
    )
    sample_owner_ids: list[str] | None = Field(
        default=None, description="Optional sample owner IDs."
    )


class ContainerDestinationContent(BaseModel):
    """One entity and optional concentration in the destination container."""

    model_config = _STRICT

    entity_id: str = Field(min_length=1, description="Entity ID in the destination contents.")
    concentration: dict[str, Any] | None = Field(
        default=None, description="Optional concentration measurement."
    )


class ContainerTransfer(BaseModel):
    """Transfer contents from one container into another after preview."""

    model_config = _STRICT

    operation: Literal["transfer"] = Field(description="Preview a container-to-container transfer.")
    source_container_id: str = Field(min_length=1, description="Source container ID.")
    destination_container_id: str = Field(min_length=1, description="Destination container ID.")
    transfer_quantity: dict[str, Any] | None = Field(
        default=None, description="Optional quantity to transfer from the source."
    )
    destination_contents: list[ContainerDestinationContent] = Field(
        min_length=1, description="Destination contents after the transfer."
    )
    destination_quantity: dict[str, Any] | None = Field(
        default=None, description="Optional desired destination quantity."
    )
    restricted_sample_party_ids: list[str] | None = Field(
        default=None, description="Optional restricted sample party IDs."
    )
    restriction_status: str | None = Field(
        default=None, description="Optional RESTRICTED or UNRESTRICTED status."
    )
    sample_owner_ids: list[str] | None = Field(
        default=None, description="Optional sample owner IDs."
    )


ChangeSpecification = (
    DocumentInsert
    | DocumentDelete
    | DocumentMove
    | DocumentReplaceText
    | TableSetCells
    | TableAppendRows
    | FieldChange
    | ResultUpload
    | MultiTableResultUpload
    | ResultCsvUpdate
    | EntryCreationSpecification
    | ContainerCreate
    | ContainerTransfer
)

_BY_OPERATION: dict[str, type[BaseModel]] = {
    "insert": DocumentInsert,
    "delete": DocumentDelete,
    "move": DocumentMove,
    "replace_text": DocumentReplaceText,
    "set_cells": TableSetCells,
    "append_rows": TableAppendRows,
    "update": ResultCsvUpdate,
    "create": ContainerCreate,
    "transfer": ContainerTransfer,
}

_SUPPORTED = [
    "document: insert, delete, move, replace_text",
    "table: set_cells, append_rows",
    "fields: schema_fields",
    "results upload: table_id and assay_results, or batches with table_id and assay_results",
    "results update: operation update, csv_path, id_column, column_map",
    "entry: entry_template_id, folder_id, name, optional schema_fields",
    "containers create: operation create and schema_id",
    "containers transfer: operation transfer, source_container_id and destination_container_id",
]


def _inline_json_schema(schema: dict[str, Any]) -> dict[str, Any]:
    """Copy a schema so every local $ref is replaced by its definition.

    MCP publishes a parameter schema on its own. A $ref to a definition that lives
    only inside this object does not resolve there.
    """
    definitions = schema.get("$defs", {})

    def resolve(node: Any) -> Any:
        if isinstance(node, dict):
            ref = node.get("$ref")
            if isinstance(ref, str) and ref.startswith("#/$defs/"):
                target = cast(
                    dict[str, Any],
                    resolve(copy.deepcopy(definitions[ref.removeprefix("#/$defs/")])),
                )
                extras = {key: resolve(value) for key, value in node.items() if key != "$ref"}
                return {**target, **extras}
            return {key: resolve(value) for key, value in node.items() if key != "$defs"}
        if isinstance(node, list):
            return [resolve(item) for item in node]
        return node

    return cast(dict[str, Any], resolve(schema))


CHANGE_SPECIFICATION_SCHEMA: dict[str, Any] = _inline_json_schema(
    TypeAdapter(ChangeSpecification).json_schema()
)


def _model_for(value: dict[str, Any]) -> type[BaseModel]:
    """Pick the one shape this object is claiming to be."""
    if "entry_template_id" in value or "folder_id" in value:
        return EntryCreationSpecification
    operation = value.get("operation")
    if isinstance(operation, str) and operation in _BY_OPERATION:
        return _BY_OPERATION[operation]
    if any(key in value for key in ("csv_path", "id_column", "column_map")):
        return ResultCsvUpdate
    has_fields = "schema_fields" in value
    has_results = "table_id" in value or "assay_results" in value
    has_batches = "batches" in value
    if has_fields and (has_results or has_batches):
        raise InputError(
            "Change specification mixes field updates and result records",
            details={"supported": _SUPPORTED},
        )
    if has_batches:
        return MultiTableResultUpload
    if has_fields:
        return FieldChange
    if has_results:
        return ResultUpload
    raise InputError(
        "Change specification does not match a supported shape",
        details={"supported": _SUPPORTED},
    )


def _error_details(exc: ValidationError) -> list[dict[str, Any]]:
    """JSON-safe locations and messages. The rejected values are not included."""
    details = []
    for err in exc.errors():
        details.append(
            {
                "path": ".".join(str(part) for part in err["loc"]),
                "message": str(err["msg"]),
                "type": str(err["type"]),
            }
        )
    return details


def _error_message(exc: ValidationError) -> str:
    """One sentence naming each invalid field."""
    pieces = []
    for err in exc.errors():
        path = ".".join(str(part) for part in err["loc"]) or "specification"
        pieces.append(f"{path}: {err['msg']}")
    return "Change specification is invalid: " + "; ".join(pieces)


def parse_change_specification(value: Any) -> dict[str, Any]:
    """Validate a changes_plan specification and return the planner's object.

    Omitted optional fields stay omitted. JSON null is kept only for projectId
    and for a field value, where null is meaningful.

    Raises InputError when the value is not one of the supported shapes.
    """
    if not isinstance(value, dict):
        raise InputError(
            "Change specification must be an object",
            details={"received": type(value).__name__},
        )
    model = _model_for(value)
    try:
        parsed = model.model_validate(value)
    except ValidationError as exc:
        raise InputError(_error_message(exc), details={"errors": _error_details(exc)}) from exc
    return parsed.model_dump(mode="json", exclude_unset=True)
