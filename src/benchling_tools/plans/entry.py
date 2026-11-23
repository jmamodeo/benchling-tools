"""Planning and application for creating notebook entries from templates."""

from __future__ import annotations

from typing import Any

from ..errors import (
    NotFoundError,
    PartialCompletionError,
    UnsupportedError,
    UpstreamError,
    ValidationError,
)
from ..listing import entry_web_url
from ..models import ChangeJournal, ChangePlan
from .base import PlanParts, write_event_details
from .fields import FieldNormalization


class EntryPlanning(FieldNormalization):
    """Create an entry after previewing and validating its template and folder."""

    async def _plan_entry_create(self, request: dict[str, Any]) -> PlanParts:
        allowed = {"entry_template_id", "folder_id", "name", "schema_fields"}
        unsupported = set(request) - allowed
        if unsupported:
            raise ValidationError(
                "Entry creation only supports entry_template_id, folder_id, name, and "
                "schema_fields",
                details={"unsupported": sorted(unsupported)},
            )

        template_id = self._required_text(request, "entry_template_id")
        folder_id = self._required_text(request, "folder_id")
        name = self._required_text(request, "name")
        try:
            template = await self.api.request("GET", f"/entry-template/{template_id}")
        except NotFoundError as exc:
            raise NotFoundError(
                "Entry template was not found",
                details={"entry_template_id": template_id},
            ) from exc
        try:
            folder = await self.api.request("GET", f"/folder/{folder_id}")
        except NotFoundError as exc:
            raise NotFoundError("Folder was not found", details={"folder_id": folder_id}) from exc
        self._validate_active_resource(template, template_id, "entry template")
        self._validate_active_resource(folder, folder_id, "folder")

        schema = template.get("schema")
        schema_id = schema.get("id") if isinstance(schema, dict) else None
        schema_fields = request.get("schema_fields")
        payload: dict[str, Any] = {
            "entryTemplateId": template_id,
            "folderId": folder_id,
            "name": name,
        }
        targets: list[dict[str, Any]] = []
        if schema_fields is not None:
            if not isinstance(schema_fields, dict) or not schema_fields:
                raise ValidationError("schema_fields must be a non-empty object when provided")
            if not isinstance(schema_id, str) or not schema_id:
                raise ValidationError("The selected entry template has no schema for schema_fields")
            schema_detail = await self.service.get_schema(schema_id)
            normalized, targets, _field_name_map = self._normalize_schema_field_updates(
                schema_fields,
                schema_detail.get("field_definitions") or [],
                address_by="field_definition_id",
            )
            payload["schemaFields"] = normalized

        template_preview = {
            "id": template_id,
            "name": template.get("name") or template_id,
            "schema_id": schema_id,
            "archived": False,
        }
        folder_preview = {
            "id": folder_id,
            "name": folder.get("name") or folder_id,
            "archived": False,
        }
        after = {
            "name": name,
            "template": template_preview,
            "folder": folder_preview,
            "schema_fields": targets,
            "request": payload,
        }
        return (
            {"template": template_preview, "folder": folder_preview},
            after,
            {"entry_template_id": template_id, "folder_id": folder_id},
            [
                "template and folder were confirmed active at preview time",
                "schema fields are sent using field-definition IDs",
                "no automatic write retry",
            ],
            [],
        )

    def _required_text(self, request: dict[str, Any], key: str) -> str:
        value = request.get(key)
        if not isinstance(value, str) or not value.strip():
            raise ValidationError(f"{key} must be a non-empty string")
        return value.strip()

    def _validate_active_resource(
        self, resource: dict[str, Any], resource_id: str, kind: str
    ) -> None:
        if resource.get("id") != resource_id:
            raise UpstreamError(
                f"Benchling returned an unexpected {kind}",
                details={"expected_id": resource_id},
            )
        archived = resource.get("archived")
        if not isinstance(archived, bool):
            raise UpstreamError(
                f"Benchling {kind} response did not include archive status",
                details={"id": resource_id},
            )
        if archived:
            raise UnsupportedError(
                f"Cannot create an entry from an archived {kind}"
                if kind == "entry template"
                else "Cannot create an entry in an archived folder",
                details={"id": resource_id},
            )

    async def _apply_entry_create(self, plan: ChangePlan, journal: ChangeJournal) -> dict[str, Any]:
        """Create one entry; the planner journal prevents replay after any attempt."""
        response = await self.api.request("POST", "/entry", version=3, json=plan.after["request"])
        entry = response.get("entry")
        created = entry if isinstance(entry, dict) else response
        entry_id = created.get("id")
        if not isinstance(entry_id, str) or not entry_id:
            raise PartialCompletionError(
                "Benchling may have created the entry, but its response did not include an ID",
                details={"http_status": response.status_code, "response_keys": sorted(response)},
            )
        result = {"entry_id": entry_id, "web_url": entry_web_url(created), "entry": created}
        self._event(
            journal,
            "create_entry",
            "succeeded",
            write_event_details(response, entry_id=entry_id),
        )
        return result
