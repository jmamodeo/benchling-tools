"""Preview and apply container creation and transfer operations."""

from __future__ import annotations

from typing import Any
from urllib.parse import quote

from ..errors import UnsupportedError, ValidationError
from ..models import ChangeJournal, ChangePlan
from .base import PlannerBase, PlanParts, write_event_details

_CREATE_FIELDS = {
    "name": "name",
    "barcode": "barcode",
    "project_id": "projectId",
    "parent_storage_id": "parentStorageId",
    "fields": "fields",
    "restricted_sample_party_ids": "restrictedSamplePartyIds",
    "restriction_status": "restrictionStatus",
    "sample_owner_ids": "sampleOwnerIds",
}

_TRANSFER_FIELDS = {
    "transfer_quantity": "transferQuantity",
    "destination_contents": "destinationContents",
    "destination_quantity": "destinationQuantity",
    "restricted_sample_party_ids": "restrictedSamplePartyIds",
    "restriction_status": "restrictionStatus",
    "sample_owner_ids": "sampleOwnerIds",
}


class ContainerPlanning(PlannerBase):
    """Planning and application of container inventory operations."""

    async def _plan_containers(self, request: dict[str, Any]) -> PlanParts:
        operation = request.get("operation")
        if operation == "create":
            schema_id = self.service._container_reference(request.get("schema_id"), "schema_id")
            schema = await self.service.get_container_schema(schema_id)
            schema_modified_at = schema.get("modifiedAt")
            definitions = schema.get("fieldDefinitions") or []
            if not isinstance(definitions, list):
                raise UnsupportedError(
                    "Container schema field definitions have an unexpected shape"
                )
            create_payload = {"schemaId": schema_id}
            create_payload.update(
                {
                    api_name: request[key]
                    for key, api_name in _CREATE_FIELDS.items()
                    if key in request and (request[key] is not None or key == "project_id")
                }
            )
            schema_snapshot = {
                "id": schema.get("id"),
                "modifiedAt": schema_modified_at,
                "fieldDefinitions": definitions,
            }
            return (
                {"schema": schema_snapshot},
                {"container": create_payload},
                {"operation": "create", "schema_id": schema_id, "schema": schema_snapshot},
                ["container schema is re-checked immediately before creation"],
                ["container creation is a one-time write; an unknown outcome is not retried"],
            )
        if operation == "transfer":
            source_id = self.service._container_reference(
                request.get("source_container_id"), "source_container_id"
            )
            destination_id = self.service._container_reference(
                request.get("destination_container_id"), "destination_container_id"
            )
            if source_id == destination_id:
                raise ValidationError("Source and destination containers must be different")
            source, destination = await self._container_pair(source_id, destination_id)
            source_modified_at = self._required_modified_at(source, "source")
            destination_modified_at = self._required_modified_at(destination, "destination")
            transfer_payload: dict[str, Any] = {
                "sourceContainerId": source_id,
                **{
                    api_name: request[key]
                    for key, api_name in _TRANSFER_FIELDS.items()
                    if key in request and request[key] is not None and key != "destination_contents"
                },
                "destinationContents": [
                    {
                        "entityId": item["entity_id"],
                        **(
                            {"concentration": item["concentration"]}
                            if item.get("concentration") is not None
                            else {}
                        ),
                    }
                    for item in request["destination_contents"]
                ],
            }
            return (
                {"source": source, "destination": destination},
                {"destination_id": destination_id, "transfer": transfer_payload},
                {
                    "operation": "transfer",
                    "source_id": source_id,
                    "source_modified_at": source_modified_at,
                    "destination_id": destination_id,
                    "destination_modified_at": destination_modified_at,
                },
                ["source and destination container versions are checked before transfer"],
                ["container transfer is a one-time write; an unknown outcome is not retried"],
            )
        raise ValidationError(
            "Container plan operation must be create or transfer",
            details={"supported": ["create", "transfer"]},
        )

    async def _container_pair(
        self, source_id: str, destination_id: str
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        source, destination = (
            await self.service.get_container(source_id),
            await self.service.get_container(destination_id),
        )
        return source, destination

    @staticmethod
    def _required_modified_at(container: dict[str, Any], label: str) -> str:
        modified_at = container.get("modifiedAt")
        if not isinstance(modified_at, str) or not modified_at:
            raise UnsupportedError(
                f"The {label} container does not expose a modification timestamp",
                details={"container_id": container.get("id")},
            )
        return modified_at

    async def _apply_containers(self, plan: ChangePlan, journal: ChangeJournal) -> dict[str, Any]:
        if plan.preconditions.get("operation") == "create":
            schema_id = plan.preconditions["schema_id"]
            schema = await self.service.get_container_schema(schema_id)
            current = {
                "id": schema.get("id"),
                "modifiedAt": schema.get("modifiedAt"),
                "fieldDefinitions": schema.get("fieldDefinitions") or [],
            }
            self._expect(current, plan.preconditions["schema"], "container schema")
            response = await self.api.request(
                "POST", "/containers", version=2, json=plan.after["container"]
            )
            self._event(journal, "create_container", "succeeded", write_event_details(response))
            return {"response": response}

        source_id = plan.preconditions["source_id"]
        destination_id = plan.preconditions["destination_id"]
        source, destination = await self._container_pair(source_id, destination_id)
        self._expect(
            self._required_modified_at(source, "source"),
            plan.preconditions["source_modified_at"],
            "source container",
        )
        self._expect(
            self._required_modified_at(destination, "destination"),
            plan.preconditions["destination_modified_at"],
            "destination container",
        )
        response = await self.api.request(
            "POST",
            f"/containers/{quote(destination_id, safe='')}:transfer",
            version=2,
            json=plan.after["transfer"],
        )
        self._event(journal, "transfer_container", "succeeded", write_event_details(response))
        return {"response": response}
