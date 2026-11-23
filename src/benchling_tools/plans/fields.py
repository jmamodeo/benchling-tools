"""Schema field planning and field value validation."""

from __future__ import annotations

from copy import deepcopy
from datetime import date, datetime
from typing import Any, Literal

from ..errors import (
    UnsupportedError,
    ValidationError,
)
from ..models import (
    ChangeJournal,
    ChangePlan,
)
from ..services import (
    field_definition_type,
    is_attachment_field,
)
from .base import PlannerBase, PlanParts, write_event_details
from .blobs import local_file_descriptor


def _resolve_field(
    token: str, definitions: list[dict[str, Any]]
) -> dict[str, Any] | list[dict[str, Any]] | None:
    """Match a field-definition id, system name, or display name."""
    by_id = [item for item in definitions if item.get("id") == token]
    if by_id:
        return by_id[0] if len(by_id) == 1 else by_id

    by_system = _prefer_live([item for item in definitions if item.get("systemName") == token])
    display_exact = _prefer_live(
        [
            item
            for item in definitions
            if isinstance(item.get("name"), str) and item["name"].strip() == token.strip()
        ]
    )
    if by_system and display_exact:
        collisions = [item for item in display_exact if item not in by_system]
        if collisions:
            return list({id(item): item for item in [*by_system, *display_exact]}.values())
    if by_system:
        return by_system[0] if len(by_system) == 1 else by_system
    if display_exact:
        return display_exact[0] if len(display_exact) == 1 else display_exact

    by_name = _prefer_live(
        [
            item
            for item in definitions
            if isinstance(item.get("name"), str)
            and item["name"].strip().casefold() == token.strip().casefold()
        ]
    )
    return by_name[0] if len(by_name) == 1 else by_name or None


def _prefer_live(matches: list[dict[str, Any]]) -> list[dict[str, Any]]:
    live = [item for item in matches if not item.get("archived")]
    return live or matches


def _available_display_names(definitions: list[dict[str, Any]]) -> dict[str, str]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    for item in definitions:
        name = item.get("name")
        if isinstance(name, str) and name.strip() and item.get("systemName"):
            grouped.setdefault(name.strip(), []).append(item)
    available: dict[str, str] = {}
    for name, matches in grouped.items():
        matches = _prefer_live(matches)
        system_names = {str(item["systemName"]) for item in matches}
        if len(system_names) == 1:
            available[name] = next(iter(system_names))
    return dict(sorted(available.items()))


class FieldNormalization(PlannerBase):
    """Validation and normalization of schema field values shared by fields and results planners."""

    def _normalize_schema_field_updates(
        self,
        updates: dict[str, Any],
        definitions: list[dict[str, Any]],
        *,
        require_all_required: bool = False,
        address_by: Literal["system_name", "field_definition_id"] = "system_name",
        local_files: bool = False,
        allow_display_names: bool = False,
        value_errors: list[ValidationError | UnsupportedError] | None = None,
    ) -> tuple[dict[str, Any], list[dict[str, Any]], dict[str, str]]:
        by_system = {
            str(item["systemName"]): item for item in definitions if item.get("systemName")
        }
        by_id = {str(item["id"]): item for item in definitions if item.get("id")}
        if not by_system:
            raise UnsupportedError(
                "The assigned schema did not expose addressable field definitions"
            )
        normalized: dict[str, Any] = {}
        targets: list[dict[str, Any]] = []
        field_name_map: dict[str, str] = {}
        unknown: list[str] = []
        seen_systems: set[str] = set()
        ambiguous_systems: set[str] = set()

        def report(exc: ValidationError | UnsupportedError) -> None:
            if value_errors is None:
                raise exc
            value_errors.append(exc)

        for supplied_key, field_input in updates.items():
            if allow_display_names:
                resolved = _resolve_field(supplied_key, definitions)
                if isinstance(resolved, list):
                    ambiguous_systems.update(
                        str(item["systemName"]) for item in resolved if item.get("systemName")
                    )
                    report(
                        ValidationError(
                            "Schema field name is ambiguous",
                            details={
                                "reason": "ambiguous_field",
                                "field": supplied_key,
                                "candidates": [
                                    {
                                        "system_name": item.get("systemName"),
                                        "field_definition_id": item.get("id"),
                                        "name": item.get("name"),
                                    }
                                    for item in resolved
                                ],
                            },
                        )
                    )
                    continue
                definition = resolved
            else:
                definition = by_system.get(supplied_key) or by_id.get(supplied_key)
            if definition is None:
                unknown.append(supplied_key)
                continue
            system_name = str(definition["systemName"])
            if supplied_key != system_name:
                field_name_map[supplied_key] = system_name
            if system_name in seen_systems:
                report(
                    ValidationError(
                        "The same schema field was supplied more than once",
                        details={"system_name": system_name},
                    )
                )
                continue
            seen_systems.add(system_name)
            if not isinstance(field_input, dict) or set(field_input) != {"value"}:
                report(
                    ValidationError(
                        "Each schema field input must contain exactly one value property",
                        details={"field": supplied_key},
                    )
                )
                continue
            if definition.get("archived"):
                report(
                    UnsupportedError(
                        "Archived schema fields cannot be updated",
                        details={"field": system_name},
                    )
                )
                continue
            if definition.get("derivationType") not in {None, "DIRECT"}:
                report(
                    UnsupportedError(
                        "Computed or snapshot schema fields cannot be updated directly",
                        details={
                            "field": system_name,
                            "derivation_type": definition.get("derivationType"),
                        },
                    )
                )
                continue
            try:
                self._validate_schema_field_value(
                    definition, field_input["value"], local_files=local_files
                )
            except (ValidationError, UnsupportedError) as exc:
                report(exc)
                continue
            field_id = definition.get("id")
            if address_by == "field_definition_id":
                if not isinstance(field_id, str) or not field_id:
                    raise UnsupportedError(
                        "Schema field has no field-definition ID",
                        details={"field": system_name},
                    )
                wire_key = field_id
            else:
                wire_key = system_name
            normalized[wire_key] = deepcopy(field_input)
            targets.append(
                {
                    "field_definition_id": field_id,
                    "system_name": system_name,
                    "name": definition.get("name"),
                    "type": field_definition_type(definition),
                }
            )
        if unknown:
            unknown_details: dict[str, Any] = {
                "unknown_fields": unknown,
                "available_system_names": sorted(by_system),
                "available_field_ids": sorted(by_id),
            }
            if allow_display_names:
                unknown_details["available_display_names"] = _available_display_names(definitions)
            report(
                ValidationError(
                    "Schema fields must be addressed by system name or field-definition ID",
                    details=unknown_details,
                )
            )
        if require_all_required:
            missing_required = sorted(
                str(item["systemName"])
                for item in definitions
                if item.get("systemName")
                and item.get("isRequired")
                and not item.get("archived")
                and item.get("derivationType") in {None, "DIRECT"}
                and str(item["systemName"]) not in seen_systems
                and str(item["systemName"]) not in ambiguous_systems
            )
            if missing_required:
                report(
                    ValidationError(
                        "Result records must provide every required schema field",
                        details={"missing_required_fields": missing_required},
                    )
                )
        return normalized, targets, field_name_map

    def _field_definition_fingerprints(
        self,
        definitions: list[dict[str, Any]],
        system_names: set[str] | None = None,
    ) -> list[dict[str, Any]]:
        fingerprints = []
        for definition in definitions:
            system_name = definition.get("systemName")
            if not system_name or (system_names is not None and system_name not in system_names):
                continue
            fingerprints.append(
                {
                    "id": definition.get("id"),
                    "system_name": system_name,
                    "type": field_definition_type(definition),
                    "is_multi": bool(definition.get("isMulti")),
                    "is_required": bool(definition.get("isRequired")),
                    "archived": bool(definition.get("archived")),
                    "derivation_type": definition.get("derivationType"),
                    "numeric_min": definition.get("numericMin"),
                    "numeric_max": definition.get("numericMax"),
                }
            )
        return sorted(fingerprints, key=lambda item: str(item["system_name"]))

    def _validate_schema_field_value(
        self, definition: dict[str, Any], value: Any, *, local_files: bool = False
    ) -> None:
        field = str(definition.get("systemName") or definition.get("id"))
        if value is None:
            if definition.get("isRequired"):
                raise ValidationError(
                    "A required schema field cannot be cleared", details={"field": field}
                )
            return
        field_type = field_definition_type(definition)
        is_multi = bool(definition.get("isMulti"))
        values = value if is_multi else [value]
        if is_multi and not isinstance(value, list):
            raise ValidationError(
                "A multi-value schema field requires an array", details={"field": field}
            )
        if is_attachment_field(definition):
            valid = all(
                (isinstance(item, str) and bool(item))
                or (local_files and local_file_descriptor(item) is not None)
                for item in values
            )
        elif field_type in {"TEXT", "LONG_TEXT"}:
            valid = all(isinstance(item, str) for item in values)
        elif field_type == "INTEGER":
            valid = all(isinstance(item, int) and not isinstance(item, bool) for item in values)
        elif field_type in {"FLOAT", "DECIMAL"}:
            valid = all(
                isinstance(item, (int, float)) and not isinstance(item, bool) for item in values
            )
        elif field_type == "BOOLEAN":
            valid = all(isinstance(item, bool) for item in values)
        elif field_type == "DATE":
            valid = all(self._is_date(item) for item in values)
        elif field_type == "DATETIME":
            valid = all(self._is_datetime(item) for item in values)
        elif field_type == "JSON":
            valid = True
        elif field_type in {"LINK", "DROPDOWN"}:
            valid = all(isinstance(item, str) and bool(item) for item in values)
        else:
            raise UnsupportedError(
                "This schema field type cannot be safely validated",
                details={"field": field, "type": field_type},
            )
        if not valid:
            raise ValidationError(
                "Schema field value has the wrong type",
                details={"field": field, "type": field_type, "is_multi": is_multi},
            )
        if field_type in {"INTEGER", "FLOAT", "DECIMAL"}:
            minimum = definition.get("numericMin")
            maximum = definition.get("numericMax")
            if minimum is not None and any(item < minimum for item in values):
                raise ValidationError(
                    "Schema field value is below its minimum",
                    details={"field": field, "minimum": minimum, "reason": "out_of_range"},
                )
            if maximum is not None and any(item > maximum for item in values):
                raise ValidationError(
                    "Schema field value is above its maximum",
                    details={"field": field, "maximum": maximum, "reason": "out_of_range"},
                )

    def _is_date(self, value: Any) -> bool:
        if not isinstance(value, str):
            return False
        try:
            return date.fromisoformat(value).isoformat() == value
        except ValueError:
            return False

    def _is_datetime(self, value: Any) -> bool:
        if not isinstance(value, str):
            return False
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return False
        return parsed.tzinfo is not None


class FieldsPlanning(FieldNormalization):
    """Planning and applying of entry schema field updates."""

    async def _plan_fields(self, entry_id: str, request: dict[str, Any]) -> PlanParts:
        updates = request.get("schema_fields")
        if not isinstance(updates, dict) or not updates:
            raise ValidationError("Field plan requires non-empty schema_fields")
        current = await self.service.get_entry_fields(entry_id)
        if not current.get("schema"):
            raise UnsupportedError("Entry has no assigned schema")
        definitions = current.get("field_definitions") or []
        normalized, targets, _field_name_map = self._normalize_schema_field_updates(
            updates, definitions, address_by="field_definition_id"
        )
        request["schema_fields"] = normalized
        fields = current.get("fields_by_system_name") or {}
        before_fields = {
            target["system_name"]: {
                "field_definition_id": target["field_definition_id"],
                "system_name": target["system_name"],
                "value": fields.get(target["system_name"]),
            }
            for target in targets
        }
        after_fields = {
            target["system_name"]: {
                "field_definition_id": target["field_definition_id"],
                "system_name": target["system_name"],
                "value": normalized[str(target["field_definition_id"])],
            }
            for target in targets
        }
        observed_values = {
            target["system_name"]: fields.get(target["system_name"]) for target in targets
        }
        schema_id = (current.get("schema") or {}).get("id")
        return (
            {"fields": before_fields, "targets": targets},
            {"fields": after_fields, "targets": targets},
            {
                "schema_id": schema_id,
                "observed_values": observed_values,
                "field_definitions": self._field_definition_fingerprints(
                    definitions, set(observed_values)
                ),
                "modified_at": current.get("modified_at"),
            },
            ["immediate pre-write field-value comparison"],
            ["Entry field updates do not expose an atomic document-version precondition"],
        )

    async def _apply_fields(self, plan: ChangePlan, journal: ChangeJournal) -> dict[str, Any]:
        entry_id = self._entry_id(plan)
        current = await self.service.get_entry_fields(entry_id)
        current_schema_id = (current.get("schema") or {}).get("id")
        self._expect(
            current_schema_id,
            plan.preconditions.get("schema_id"),
            "entry schema",
        )
        self._expect(
            self._field_definition_fingerprints(
                current.get("field_definitions") or [],
                set(plan.preconditions["observed_values"]),
            ),
            plan.preconditions.get("field_definitions"),
            "entry field definitions",
        )
        observed = {
            key: (current.get("fields_by_system_name") or {}).get(key)
            for key in plan.preconditions["observed_values"]
        }
        self._expect(observed, plan.preconditions["observed_values"], "entry field values")
        response = await self.api.request(
            "PATCH",
            f"/entry/{entry_id}",
            json={"schemaFields": plan.request["schema_fields"]},
        )
        self._event(journal, "update_fields", "succeeded", write_event_details(response))
        return {"response": response, "concurrency": "best_effort_value_comparison"}
