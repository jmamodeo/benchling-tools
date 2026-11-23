"""Result-record submission planning."""

from __future__ import annotations

import uuid
from copy import deepcopy
from pathlib import Path
from typing import Any

from ..errors import (
    AccessError,
    BenchlingAgentError,
    ConflictError,
    NotFoundError,
    PartialCompletionError,
    UnsupportedError,
    ValidationError,
)
from ..models import (
    ChangeJournal,
    ChangePlan,
)
from ..services import field_definition_type, is_attachment_field
from . import readback
from .base import PlanParts, write_event_details
from .blobs import (
    UPLOAD_WARNINGS,
    FileStager,
    discard_refs,
    local_file_descriptor,
    plan_upload_refs,
    stage_field_values,
)
from .fields import _resolve_field
from .result_update import ResultUpdatePlanning, request_is_result_update

PENDING_RESULTS_WARNING = (
    "Created results stay pending until a person accepts them in the entry's results table "
    "in Benchling (the plug icon in the table toolbar)"
)


def plan_entry_web_url(plan: ChangePlan) -> str | None:
    """The entry's web-app link recorded with a results upload plan's tables, if any."""
    before = plan.before or {}
    tables = before.get("tables")
    candidates = tables if isinstance(tables, list) else [before]
    for item in candidates:
        table = item.get("table") if isinstance(item, dict) else None
        if isinstance(table, dict):
            url = table.get("entry_web_url")
            if isinstance(url, str) and url:
                return url
    return None


def pending_results_next_step(
    plan: ChangePlan, table_ids: set[str] | None = None, *, web_url: str | None = None
) -> str | None:
    """Tell the user how to accept pending results, or None when the plan created none."""
    if plan.family.value != "results" or plan.after.get("state") != "pending_integration":
        return None
    tables = (plan.before or {}).get("tables")
    if isinstance(tables, list):
        names = [
            str((item.get("table") or {}).get("name") or item.get("table_id"))
            for item in tables
            if isinstance(item, dict) and (table_ids is None or item.get("table_id") in table_ids)
        ]
    else:
        table = (plan.before or {}).get("table") or {}
        table_id = plan.after.get("table_id")
        names = (
            [str(table.get("name") or table_id)]
            if table_ids is None or table_id in table_ids
            else []
        )
    if not names:
        return None
    name = ", ".join(names)
    noun = "tables" if len(names) > 1 else "table"
    url = web_url or plan_entry_web_url(plan)
    where = f"entry {plan.entry_id} at {url}" if url else f"entry {plan.entry_id}"
    return (
        f'Results are pending. Open {where}, go to the result {noun} "{name}", '
        "and click the plug (integrate) icon in each table's toolbar to accept the pending results. "
        "Until you do, they do not appear in the table and cannot be updated with this "
        "toolkit, because an update needs a result that is on the entry. Refresh the page "
        "if the entry is already open. To check, re-read with `results list --entry` or "
        "`results rows`."
    )


class ResultsPlanning(ResultUpdatePlanning):
    """Planning of new result records and updates to existing ones."""

    async def _plan_results(self, entry_id: str, request: dict[str, Any]) -> PlanParts:
        if request_is_result_update(request):
            if "table_id" in request:
                raise ValidationError("A result update does not use table_id or assay_results")
            if any(key in request for key in ("assay_results", "batches")):
                raise ValidationError("A result update does not use table or upload fields")
            return await self._plan_result_updates(entry_id, request)
        stager = FileStager(self.repository.config, Path.cwd())
        try:
            return await self._plan_result_uploads(entry_id, request, stager)
        except BaseException:
            stager.discard()
            raise

    async def _plan_result_uploads(
        self, entry_id: str, request: dict[str, Any], stager: FileStager
    ) -> PlanParts:
        if "batches" in request:
            if "table_id" in request or "assay_results" in request:
                raise ValidationError("Pass batches or table_id and assay_results, not both")
            batches = request["batches"]
        else:
            batches = [
                {"table_id": request.get("table_id"), "assay_results": request.get("assay_results")}
            ]
        if not isinstance(batches, list) or not batches:
            raise ValidationError("Result plan requires non-empty batches")
        planned_batches: list[dict[str, Any]] = []
        seen_tables: set[str] = set()
        plan_row_offset = 0
        row_problems: list[dict[str, Any]] = []
        linked_schema_cache: dict[str, str | None] = {}
        entry_project_id: str | None = None
        project_source = "entry_folder"
        project_resolved = False
        project_defaults: list[dict[str, Any]] = []
        for batch in batches:
            if not isinstance(batch, dict):
                raise ValidationError("Each result batch requires table_id and assay_results")
            if set(batch) != {"table_id", "assay_results"}:
                raise ValidationError("Each result batch only accepts table_id and assay_results")
            table_id = batch.get("table_id")
            records = batch.get("assay_results")
            if not isinstance(table_id, str) or not table_id.startswith("strtbl_"):
                raise ValidationError("Each result batch requires an explicit V2 result table ID")
            if table_id in seen_tables:
                raise ValidationError("A result plan cannot include the same table more than once")
            seen_tables.add(table_id)
            if not isinstance(records, list) or not records:
                raise ValidationError("Each result batch requires non-empty assay_results")
            if len(records) > 500:
                raise ValidationError(
                    "Benchling accepts at most 500 results per notebook table upload"
                )
            table = await self.service.get_result_table(entry_id, table_id)
            schema_id = table.get("schema_id")
            if not isinstance(schema_id, str):
                raise UnsupportedError(
                    "The selected result table does not expose a result schema",
                    details={"table_id": table_id},
                )
            schema = await self.service.get_schema(schema_id)
            definitions = schema.get("field_definitions") or []
            default_rows = [
                i
                for i, record in enumerate(records)
                if isinstance(record, dict) and "projectId" not in record
            ]
            if default_rows and not project_resolved:
                entry = self.service._v2_object(
                    await self.api.request("GET", f"/entries/{entry_id}", version=2), "entry"
                )
                try:
                    entry_project_id = await self.service._entry_project_id(entry)
                except (AccessError, NotFoundError):
                    entry_project_id = None
                if isinstance(entry.get("project"), dict) and entry["project"].get("id"):
                    project_source = "entry_project"
                project_resolved = True
            if default_rows:
                if not isinstance(entry_project_id, str) or not entry_project_id.strip():
                    raise ValidationError(
                        "A project is required to plan these results",
                        details={
                            "reason": "project_required",
                            "hint": 'Set projectId on each record (src_…), or pass "projectId": null to create creator-only results.',
                        },
                    )
                project_defaults.append(
                    {
                        "table_id": table_id,
                        "row_indexes": default_rows,
                        "project_id": entry_project_id,
                        "source": project_source,
                    }
                )
            (
                planned,
                client_record_ids,
                projects,
                problems,
                field_name_maps,
            ) = await self._prepare_result_records(
                records,
                table_id,
                schema_id,
                definitions,
                linked_schema_cache=linked_schema_cache,
                plan_row_offset=plan_row_offset,
                stager=stager,
                default_project_id=entry_project_id,
            )
            row_problems.extend(problems)
            planned_batches.append(
                {
                    "table_id": table_id,
                    "schema_id": schema_id,
                    "table": table,
                    "records": planned,
                    "field_name_maps": field_name_maps,
                    "client_record_ids": client_record_ids,
                    "projects": projects,
                    "row_count": len(planned),
                    "plan_row_offset": plan_row_offset,
                    "schema_fields": self._field_definition_fingerprints(definitions),
                }
            )
            plan_row_offset += len(planned)
        validation_summary = self._result_validation_summary(plan_row_offset, row_problems)
        if row_problems:
            first = row_problems[0]
            raise ValidationError(
                f"Result plan contains invalid rows: row {first['row_index']}: {first['message']}",
                details={
                    "row_problems": row_problems,
                    "validation_summary": validation_summary,
                },
            )
        if "batches" in request:
            for source_batch, planned_batch in zip(
                request["batches"], planned_batches, strict=True
            ):
                for source_record, planned_record in zip(
                    source_batch["assay_results"], planned_batch["records"], strict=True
                ):
                    source_record["fields"] = planned_record["fields"]
        else:
            for source_record, planned_record in zip(
                request["assay_results"], planned_batches[0]["records"], strict=True
            ):
                source_record["fields"] = planned_record["fields"]
        all_records = [record for batch in planned_batches for record in batch["records"]]
        all_projects = {
            project_id: project
            for batch in planned_batches
            for project_id, project in batch["projects"].items()
        }
        before = {
            "tables": [
                {
                    "table_id": batch["table_id"],
                    "schema_id": batch["schema_id"],
                    "row_count": batch["row_count"],
                    "table": batch["table"],
                }
                for batch in planned_batches
            ],
            "records": [],
        }
        after = {
            "batches": planned_batches,
            "projects": all_projects,
            "state": "pending_integration",
            "validation_summary": validation_summary,
            "project_defaults": project_defaults,
        }
        preconditions: dict[str, Any] = {
            "entry_id": entry_id,
            "batches": [
                {
                    "table_id": batch["table_id"],
                    "schema_id": batch["schema_id"],
                    "schema_fields": batch["schema_fields"],
                }
                for batch in planned_batches
            ],
        }
        if len(planned_batches) == 1:
            only = planned_batches[0]
            before["table"] = only["table"]
            after.update(
                {
                    "table_id": only["table_id"],
                    "schema_id": only["schema_id"],
                    "records": only["records"],
                    "field_name_maps": only["field_name_maps"],
                    "client_record_ids": only["client_record_ids"],
                }
            )
            preconditions.update(
                {
                    "table_id": only["table_id"],
                    "result_schema_id": only["schema_id"],
                    "result_schema_fields": only["schema_fields"],
                }
            )
        warnings = self._result_plan_warnings(all_records)
        guarantees = [
            "verified entry-to-table and table-to-schema relationships",
            "local correlation UUIDs and persisted task IDs for reconciliation",
            "no automatic write retry",
        ]
        if stager.uploads:
            after["uploads"] = stager.uploads
            warnings.extend(UPLOAD_WARNINGS)
            guarantees.append("each local file's size and SHA-256 are checked again before upload")
        return (
            before,
            after,
            preconditions,
            guarantees,
            warnings,
        )

    async def _prepare_result_records(
        self,
        records: list[Any],
        table_id: str,
        schema_id: str,
        definitions: list[Any],
        *,
        linked_schema_cache: dict[str, str | None],
        plan_row_offset: int,
        stager: FileStager | None = None,
        default_project_id: str | None = None,
    ) -> tuple[
        list[Any],
        list[str],
        dict[str, dict[str, Any]],
        list[dict[str, Any]],
        list[dict[str, Any]],
    ]:
        """Validate and normalize records; returns them with correlation IDs and referenced projects."""
        planned: list[Any] = []
        client_record_ids: list[str] = []
        projects: dict[str, dict[str, Any]] = {}
        problems: list[dict[str, Any]] = []
        field_name_maps: list[dict[str, Any]] = []
        for row_index, raw_record in enumerate(records):
            record = deepcopy(raw_record)
            field_name_map: dict[str, str] = {}
            try:
                if (
                    not isinstance(record, dict)
                    or not record.get("schemaId")
                    or not isinstance(record.get("fields"), dict)
                ):
                    raise ValidationError("Each result requires schemaId and fields")
                if record["schemaId"] != schema_id:
                    raise ValidationError(
                        "A result record's schema does not match the selected table",
                        details={
                            "table_id": table_id,
                            "expected_schema_id": schema_id,
                            "actual_schema_id": record["schemaId"],
                        },
                    )
                if stager is not None:
                    stage_name_map = {
                        key: resolved["systemName"]
                        for key in raw_record["fields"]
                        if isinstance((resolved := _resolve_field(key, definitions)), dict)
                    }
                    stage_field_values(
                        raw_record["fields"],
                        definitions,
                        stager,
                        field_name_map=stage_name_map,
                        table_id=table_id,
                        row_index=plan_row_offset + row_index,
                    )
                    record["fields"] = deepcopy(raw_record["fields"])
                normalized, _, field_name_map = self._normalize_schema_field_updates(
                    record["fields"],
                    definitions,
                    require_all_required=True,
                    local_files=stager is not None,
                    allow_display_names=True,
                )
                await self._validate_result_field_constraints(
                    normalized, definitions, linked_schema_cache
                )
                record["fields"] = normalized
                if "id" in record:
                    raise UnsupportedError(
                        "Notebook table uploads use server-assigned result IDs; omit id"
                    )
                if "projectId" not in record and default_project_id is not None:
                    record["projectId"] = default_project_id
                project_id = record.get("projectId")
                if project_id is not None:
                    if not isinstance(project_id, str) or not project_id.strip():
                        raise ValidationError("projectId must be a non-empty string or null")
                    if project_id not in projects:
                        project = await self.service.get_project(project_id)
                        projects[project_id] = {
                            "id": project.get("id", project_id),
                            "name": project.get("name"),
                        }
            except (ValidationError, UnsupportedError) as exc:
                reason = exc.details.get("reason")
                if not isinstance(reason, str):
                    reason = (
                        "invalid_field_value"
                        if isinstance(exc, ValidationError)
                        and "schema field" in exc.message.lower()
                        else "invalid_row"
                    )
                problems.append(
                    {
                        "row_index": plan_row_offset + row_index,
                        "batch_row_index": row_index,
                        "table_id": table_id,
                        "reason": reason,
                        "message": exc.message,
                        "details": exc.details,
                    }
                )
            planned.append(record)
            client_record_ids.append(str(uuid.uuid4()))
            field_name_maps.append(
                {
                    "row_index": plan_row_offset + row_index,
                    "field_name_map": field_name_map,
                }
            )
        return planned, client_record_ids, projects, problems, field_name_maps

    async def _validate_result_field_constraints(
        self,
        fields: dict[str, Any],
        definitions: list[dict[str, Any]],
        linked_schema_cache: dict[str, str | None],
    ) -> None:
        for definition in definitions:
            system_name = definition.get("systemName")
            if not isinstance(system_name, str) or system_name not in fields:
                continue
            value = fields[system_name].get("value")
            if value is None or is_attachment_field(definition):
                continue
            values = value if isinstance(value, list) else [value]
            field_type = field_definition_type(definition)
            if field_type == "DROPDOWN":
                dropdown = definition.get("dropdown")
                options = dropdown.get("options") if isinstance(dropdown, dict) else None
                option_ids = {
                    item.get("id")
                    for item in options or []
                    if isinstance(item, dict) and isinstance(item.get("id"), str)
                }
                if not option_ids:
                    raise ValidationError(
                        "Dropdown field options are unavailable for validation",
                        details={"reason": "dropdown_options_unavailable", "field": system_name},
                    )
                if any(item not in option_ids for item in values):
                    raise ValidationError(
                        "Dropdown value is not one of the field's options",
                        details={"reason": "invalid_dropdown_value", "field": system_name},
                    )
            elif field_type == "LINK":
                link_definition = definition.get("linkDefinition")
                allowed_schema_id = (
                    link_definition.get("id") if isinstance(link_definition, dict) else None
                )
                if not isinstance(allowed_schema_id, str) or not allowed_schema_id:
                    raise ValidationError(
                        "Linked field does not expose an allowed schema",
                        details={
                            "reason": "link_definition_unavailable",
                            "field": system_name,
                        },
                    )
                for entity_id in values:
                    if entity_id not in linked_schema_cache:
                        try:
                            linked_schema_cache[
                                entity_id
                            ] = await self.service.get_linked_entity_schema_id(entity_id)
                        except NotFoundError:
                            linked_schema_cache[entity_id] = None
                    actual_schema_id = linked_schema_cache[entity_id]
                    if actual_schema_id is None:
                        raise ValidationError(
                            "Linked entity was not found",
                            details={
                                "reason": "link_target_not_found",
                                "field": system_name,
                                "entity_id": entity_id,
                            },
                        )
                    if actual_schema_id != allowed_schema_id:
                        raise ValidationError(
                            "Linked entity does not use a schema allowed by this field",
                            details={
                                "reason": "invalid_link_target",
                                "field": system_name,
                                "entity_id": entity_id,
                                "expected_schema_id": allowed_schema_id,
                                "actual_schema_id": actual_schema_id,
                            },
                        )

    @staticmethod
    def _result_validation_summary(
        rows_checked: int, problems: list[dict[str, Any]]
    ) -> dict[str, Any]:
        counts: dict[str, int] = {}
        for problem in problems:
            reason = problem["reason"]
            counts[reason] = counts.get(reason, 0) + 1
        return {
            "rows_checked": rows_checked,
            "rows_with_problems": len({problem["row_index"] for problem in problems}),
            "counts_by_reason": counts,
        }

    def _result_plan_warnings(self, planned: list[Any]) -> list[str]:
        """Warnings shown in the preview for submitted results."""
        warnings = [
            PENDING_RESULTS_WARNING,
            "Result IDs are assigned by Benchling; local correlation UUIDs are not idempotency keys",
        ]
        if any(record.get("projectId") is None for record in planned):
            warnings.append(
                "Results with projectId null are creator-only until access is changed; "
                "some tenants reject them at apply with projectId is a required field"
            )
        return warnings

    async def _apply_results(self, plan: ChangePlan, journal: ChangeJournal) -> dict[str, Any]:
        refs = plan_upload_refs(plan)
        try:
            if plan.preconditions.get("operation") == "update":
                return await self._apply_result_updates(plan, journal, refs)
            return await self._apply_result_uploads(plan, journal, refs)
        finally:
            discard_refs(self.repository.config, sorted(refs))

    async def _apply_result_uploads(
        self, plan: ChangePlan, journal: ChangeJournal, refs: set[str]
    ) -> dict[str, Any]:
        entry_id = self._entry_id(plan)
        uploaded: dict[str, dict[str, Any]] = {}
        batches = plan.after.get("batches")
        if not isinstance(batches, list):
            batches = [
                {
                    "table_id": plan.after["table_id"],
                    "schema_id": plan.preconditions["result_schema_id"],
                    "records": plan.after["records"],
                    "client_record_ids": plan.after.get("client_record_ids", []),
                    "schema_fields": plan.preconditions.get("result_schema_fields"),
                }
            ]
        expected = {
            batch["table_id"]: batch
            for batch in plan.preconditions.get("batches", [])
            if isinstance(batch, dict)
        }
        if not expected:
            expected = {
                plan.preconditions["table_id"]: {
                    "schema_id": plan.preconditions["result_schema_id"],
                    "schema_fields": plan.preconditions.get("result_schema_fields"),
                }
            }
        web_url = plan_entry_web_url(plan)
        for batch in batches:
            table_id = batch["table_id"]
            if any("id" in record for record in batch["records"]):
                raise ConflictError(
                    "This result plan uses an unsupported bulk-table contract; create a new preview"
                )
            if any(
                "projectId" not in record
                or (
                    record["projectId"] is not None
                    and (
                        not isinstance(record["projectId"], str) or not record["projectId"].strip()
                    )
                )
                for record in batch["records"]
            ):
                raise ConflictError(
                    "This result plan has a missing or invalid projectId; create a new preview"
                )
            table = await self.service.get_result_table(entry_id, table_id)
            web_url = table.get("entry_web_url") or web_url
            self._expect(
                table.get("schema_id"), expected[table_id]["schema_id"], "result table schema"
            )
            schema = await self.service.get_schema(expected[table_id]["schema_id"])
            self._expect(
                self._field_definition_fingerprints(schema.get("field_definitions") or []),
                expected[table_id].get("schema_fields"),
                "result schema fields",
            )
            for record in batch["records"]:
                for field in record["fields"].values():
                    value = field.get("value") if isinstance(field, dict) else None
                    for item in value if isinstance(value, list) else [value]:
                        descriptor = local_file_descriptor(item)
                        if descriptor is not None and descriptor["ref"] in refs:
                            self._read_planned_file(descriptor)
        submitted: list[dict[str, Any]] = []
        failures: list[dict[str, Any]] = []
        orphaned_blob_ids: list[str] = []
        uncertain = False
        first_error: BenchlingAgentError | None = None
        for batch_index, batch in enumerate(batches):
            table_id = batch["table_id"]
            uploaded_before = set(readback.uploaded_blob_ids(journal))
            create_attempted = False
            try:
                records = batch["records"]
                if refs:
                    records = [
                        {
                            **record,
                            "fields": await self._link_local_files(
                                record["fields"], refs, journal, uploaded
                            ),
                        }
                        for record in records
                    ]
                create_attempted = True
                response = await self.api.request(
                    "POST",
                    "/assay-results:bulk-create",
                    version=2,
                    json={"tableId": table_id, "assayResults": records},
                )
            except PartialCompletionError as exc:
                if not create_attempted:
                    orphaned_blob_ids.extend(
                        blob_id
                        for ref, blob_id in readback.uploaded_blob_ids(journal).items()
                        if ref not in uploaded_before
                    )
                uncertain = True
                first_error = first_error or exc
                failure = {
                    "batch_index": batch_index,
                    "table_id": table_id,
                    "state": "unknown",
                    "error": exc.message,
                    "details": exc.details,
                }
                failures.append(failure)
                self._event(journal, "create_results", "unknown", failure)
                continue
            except BenchlingAgentError as exc:
                orphaned_blob_ids.extend(
                    blob_id
                    for ref, blob_id in readback.uploaded_blob_ids(journal).items()
                    if ref not in uploaded_before
                )
                first_error = first_error or exc
                failure = {
                    "batch_index": batch_index,
                    "table_id": table_id,
                    "state": "failed",
                    "error": exc.message,
                    "details": exc.details,
                }
                failures.append(failure)
                self._event(journal, "create_results", "failed", failure)
                continue
            task_id = response.get("taskId") or response.get("id")
            if not task_id:
                uncertain = True
                first_error = first_error or PartialCompletionError(
                    "Benchling accepted the result upload but did not return a task ID"
                )
                failure = {
                    "batch_index": batch_index,
                    "table_id": table_id,
                    "state": "unknown",
                    "response": dict(response),
                    "client_record_ids": batch.get("client_record_ids", []),
                }
                failures.append(failure)
                self._event(
                    journal,
                    "create_results",
                    "unknown",
                    {**write_event_details(response), **failure},
                )
                continue
            result = {
                "batch_index": batch_index,
                "table_id": table_id,
                "task_id": task_id,
                "response": dict(response),
                "record_ids": [],
                "client_record_ids": batch.get("client_record_ids", []),
                "row_count": len(batch["records"]),
                "plan_row_offset": batch.get("plan_row_offset", 0),
                "state": "submission_pending",
            }
            recorded = write_event_details(
                response, **{key: value for key, value in result.items() if key != "response"}
            )
            if "http_status" in recorded:
                result["http_status"] = recorded["http_status"]
            self._event(journal, "create_results", "succeeded", recorded)
            submitted.append(result)
        if failures:
            details: dict[str, Any] = {
                "submitted": submitted,
                "failures": failures,
                "client_record_ids": [
                    record_id
                    for batch in batches
                    for record_id in batch.get("client_record_ids", [])
                ],
            }
            if orphaned_blob_ids:
                details["orphaned_blob_ids"] = orphaned_blob_ids
                details["next_step"] = (
                    "The listed blobs were uploaded but are unlinked. The V2 Blobs API has no "
                    "delete operation; a new plan will upload the files again."
                )
            if uncertain:
                raise PartialCompletionError(
                    "One or more result table uploads have an unconfirmed outcome",
                    details=details,
                ) from first_error
            assert first_error is not None
            first_error.details = {**first_error.details, **details}
            raise first_error
        result = {
            "batches": submitted,
            "state": "submission_pending",
            "web_url": web_url,
        }
        if uploaded:
            result["blobs"] = list(uploaded.values())
        if len(submitted) == 1:
            result.update(
                {
                    "task_id": submitted[0]["task_id"],
                    "response": submitted[0]["response"],
                    "record_ids": [],
                    "client_record_ids": submitted[0]["client_record_ids"],
                    "http_status": submitted[0]["http_status"],
                }
            )
        return result
