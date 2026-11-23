"""A small stateful, offline stand-in for the Benchling REST API.

It is served through ``httpx.MockTransport`` so the real client, service and
planner code runs unmodified. All identifiers and URLs are synthetic.
"""

from __future__ import annotations

import base64
import hashlib
import json
import re
from copy import deepcopy
from dataclasses import dataclass
from typing import Any
from urllib.parse import parse_qs, urlsplit

import httpx

ENTRY_ID = "etr_abc123"
TABLE_PART = "docpart_table1"
TEXT_PART = "docpart_text1"
TENANT = "https://your-tenant-dev.benchling.com"


def entry_web_url(entry_id: str) -> str:
    """A synthetic Benchling web-app link for an entry on the fake tenant."""
    return f"{TENANT}/benchling/f/lib_abc123-notebook/{entry_id}-synthetic/edit"


@dataclass
class Call:
    method: str
    path: str
    params: dict[str, str]
    body: Any


@dataclass
class Fault:
    method: str
    contains: str
    status: int
    skip: int
    times: int


def text_part(part_id: str, text: str, **extra: Any) -> dict[str, Any]:
    return {
        "id": part_id,
        "content": {
            "__typename": "TextDocumentContent",
            "text": text,
            "formats": [],
            "links": [],
            "mentions": [],
            "indentation": 0,
            **extra,
        },
    }


def text_cell(value: str, **extra: Any) -> dict[str, Any]:
    return {"inputValue": {"type": "text_value", "value": value}, **extra}


def default_results_rows() -> list[dict[str, Any]]:
    """Three results-table rows, enough to exercise a short page and a last page."""
    return [
        {
            "id": "strow_1",
            "rowId": "row_1",
            "submissionStatus": "SUBMITTED",
            "isSubmittable": False,
            "result": {"__typename": "Result", "id": "result_1"},
            "cells": [{"inputValue": {"__typename": "IntegerValue", "value": 12}}],
        },
        {
            "id": "strow_2",
            "rowId": "row_2",
            "submissionStatus": "UNSUBMITTED",
            "isSubmittable": True,
            "cells": [{"inputValue": {"__typename": "DecimalValue", "value": "1.5"}}],
        },
        {
            "id": "strow_3",
            "rowId": "row_3",
            "submissionStatus": "HAS_UNSUBMITTED_CHANGES",
            "cells": [{"inputValue": {"__typename": "TextValue", "value": "ok"}}],
        },
    ]


def evaluated_results_rows() -> list[dict[str, Any]]:
    """Accepted rows from a web-app-evaluated results table with link columns.

    Cells cover ObjectLinkCellEvaluationValue, an unknown evaluation typename,
    a BenchlingValue with no `value`, and a cell `errors` list.
    """
    return [
        {
            "id": "strow_e1",
            "rowId": "row_e1",
            "submissionStatus": "SUBMITTED",
            "result": {"__typename": "Result", "id": "result_e1"},
            "cells": [
                {
                    "inputValue": {"__typename": "ObjectLinkValue", "value": "seq_example1"},
                    "evaluationValue": {
                        "__typename": "ObjectLinkCellEvaluationValue",
                        "id": "seq_example1",
                        "name": "Example sequence",
                    },
                },
                {
                    "inputValue": {"__typename": "ObjectLinkValue", "value": "box_example1"},
                    "evaluationValue": {
                        "__typename": "ObjectLinkCellEvaluationValue",
                        "id": "box_example1",
                        "name": "Example box",
                    },
                },
                {"inputValue": {"__typename": "TextValue", "value": "first"}},
            ],
        },
        {
            "id": "strow_e2",
            "rowId": "row_e2",
            "submissionStatus": "SUBMITTED",
            "result": {"id": "result_e2"},
            "cells": [
                {
                    "inputValue": {"__typename": "ObjectLinkValue", "value": "seq_example2"},
                    "evaluationValue": {"__typename": "FutureCellEvaluationValueKind", "x": 1},
                },
                {"inputValue": {"__typename": "ObjectLinkValue"}},
                {
                    "inputValue": {"__typename": "TextValue"},
                    "evaluationValue": {"__typename": "TextValue"},
                    "errors": [
                        {
                            "__typename": "CellError",
                            "errorType": "VALIDATION",
                            "message": "Field is required",
                            "type": "CellError",
                        }
                    ],
                },
            ],
        },
    ]


def _offset_stamp(value: str) -> str:
    """V2 listing timestamps use a numeric offset. ``Z`` becomes ``+00:00``."""
    if value.endswith("Z"):
        return f"{value[:-1]}+00:00"
    return value


def default_table_content() -> dict[str, Any]:
    return {
        "__typename": "UnstructuredTableDocumentContent",
        "name": "Measurements",
        "columnDefinitions": [{"name": "Sample"}, {"name": "Value"}],
        "isEvaluated": True,
        "rows": [
            {
                "cells": [
                    text_cell("A"),
                    text_cell(
                        "1",
                        evaluatedValue={"type": "text_value", "value": "1"},
                        format={"backgroundColor": "#fff000"},
                        selectorId="selector_1",
                    ),
                ]
            },
            {
                "cells": [
                    text_cell("B", mergeRange={"mergeRowSpan": 1, "mergeColSpan": 2}),
                    text_cell("", mergeRangeParent={"row": 1, "col": 0}),
                ]
            },
            {"cells": [text_cell("C"), text_cell("=SUM(A1:A2)")]},
        ],
    }


def field_definitions() -> list[dict[str, Any]]:
    base = {"derivationType": "DIRECT"}
    return [
        {
            **base,
            "id": "tsf_temp",
            "systemName": "temperature",
            "name": "Temperature",
            "__typename": "IntegerFieldDefinition",
            "isRequired": True,
            "numericMin": 0,
            "numericMax": 100,
        },
        {**base, "id": "tsf_notes", "systemName": "notes", "name": "Notes", "type": "text"},
        {
            **base,
            "id": "tsf_ratio",
            "systemName": "ratio",
            "name": "Ratio",
            "__typename": "FloatFieldDefinition",
            "numericMin": 0.5,
        },
        {
            **base,
            "id": "tsf_flag",
            "systemName": "flag",
            "name": "Flag",
            "__typename": "BooleanFieldDefinition",
        },
        {
            **base,
            "id": "tsf_day",
            "systemName": "run_date",
            "name": "Run date",
            "__typename": "DateFieldDefinition",
        },
        {
            **base,
            "id": "tsf_start",
            "systemName": "started_at",
            "name": "Started at",
            "__typename": "DateTimeFieldDefinition",
        },
        {
            **base,
            "id": "tsf_tags",
            "systemName": "tags",
            "name": "Tags",
            "__typename": "TextFieldDefinition",
            "isMulti": True,
        },
        {
            **base,
            "id": "tsf_meta",
            "systemName": "meta",
            "name": "Meta",
            "__typename": "JsonFieldDefinition",
        },
        {
            **base,
            "id": "tsf_link",
            "systemName": "sample_link",
            "name": "Sample link",
            "__typename": "EntityLinkFieldDefinition",
        },
        {
            **base,
            "id": "tsf_weird",
            "systemName": "weird",
            "name": "Weird",
            "__typename": "MysteryFieldDefinition",
        },
        {
            "id": "tsf_calc",
            "systemName": "calculated",
            "name": "Calculated",
            "__typename": "IntegerFieldDefinition",
            "derivationType": "CALCULATED",
        },
        {
            **base,
            "id": "tsf_old",
            "systemName": "old",
            "name": "Old",
            "__typename": "TextFieldDefinition",
            "archived": True,
        },
    ]


def _result_entry_id(item: dict[str, Any]) -> str | None:
    """V2 ``entryId`` or the V3 ``entry`` ref."""
    entry_id = item.get("entryId")
    if isinstance(entry_id, str):
        return entry_id
    entry = item.get("entry")
    if isinstance(entry, dict) and isinstance(entry.get("id"), str):
        return entry["id"]
    return None


def _v3_view(item: dict[str, Any]) -> dict[str, Any]:
    """The V3 ``Result`` for a V2 assay result: id is ``v3Id``; no V2 UUID is included."""
    entry_id = item.get("entryId")
    return {
        "__typename": "Result",
        "id": item["v3Id"],
        "archived": bool(item.get("archiveRecord")),
        "createdAt": item.get("createdAt"),
        "entry": {"__typename": "DocumentLikeRef", "id": entry_id} if entry_id else None,
        "schema": {"__typename": "ResultSchemaRef", "id": (item.get("schema") or {}).get("id")},
        "schemaFields": [],
    }


def _result_schema_id(item: dict[str, Any]) -> str | None:
    """V2 ``schemaId`` or the V3 ``schema`` ref."""
    schema_id = item.get("schemaId")
    if isinstance(schema_id, str):
        return schema_id
    schema = item.get("schema")
    if isinstance(schema, dict) and isinstance(schema.get("id"), str):
        return schema["id"]
    return None


class FakeBenchling:
    def __init__(self) -> None:
        self.calls: list[Call] = []
        self.users: list[dict[str, Any]] = [
            {
                "id": "ent_fixture1",
                "handle": "fixture_handle_1",
                "name": "Fixture User 1",
                "email": "fixture1@example.test",
                "isSuspended": False,
                "memberOf": ["org_fixture"],
            },
            {
                "id": "ent_fixture2",
                "handle": "fixture_handle_2",
                "name": "Fixture User 2",
                "email": "fixture2@example.test",
                "isSuspended": True,
                "memberOf": ["org_other"],
            },
        ]
        self.origin = TENANT
        self.faults: list[Fault] = []
        self.token_status = 200
        self.token_body: dict[str, Any] | None = None
        self.access_token = "token"
        self.stamp: str | None = None
        self.entry_version = 1
        self.entry_name = "Synthetic experiment"
        self.display_id = "EXP-1"
        self.entry_templates: dict[str, dict[str, Any]] = {
            "etpl_abc123": {
                "id": "etpl_abc123",
                "name": "Synthetic template",
                "archived": False,
                "schema": {"id": "entsch_abc123"},
                "days": [
                    {
                        "title": "Introduction",
                        "notes": [
                            {"type": "text", "text": "Template instructions"},
                            {"type": "code", "text": "measure()"},
                            {
                                "type": "results_table",
                                "apiId": "strtbl_created1",
                                "name": "Template results",
                                "assayResultSchemaId": "assaysch_abc123",
                                "columns": ["ct"],
                            },
                            {"type": "table", "table": {"name": "Sample metadata"}},
                            {"type": "text_box", "text": "Box preview"},
                        ],
                    },
                    {"title": "Results", "notes": []},
                ],
            }
        }
        self.folders: dict[str, dict[str, Any]] = {
            "lib_abc123": {"id": "lib_abc123", "name": "Synthetic folder", "archived": False}
        }
        self.created_entries: dict[str, dict[str, Any]] = {}
        self.parts: list[dict[str, Any]] = [
            {"id": "docpart_sec1", "__typename": "DocumentPartSection", "title": "Methods"},
            text_part(
                TEXT_PART,
                "before",
                formats=[{"start": 0, "end": 3, "properties": ["BOLD"]}],
            ),
            {"id": "docpart_sec2", "__typename": "DocumentPartSection", "title": "Results"},
            {
                "id": TABLE_PART,
                "content": {"__typename": "UnstructuredTableDocumentContentSummary"},
            },
            {
                "id": "docpart_struct1",
                "content": {"__typename": "ResultsStructuredTableDocumentContent"},
            },
            text_part("docpart_text2", "after"),
        ]
        self.table_content = default_table_content()
        self.table_page_size: int | None = None
        self.rows_field: Any = None
        self.row_count_override: int | None = None
        self.omit_table_version = False
        self.omit_entry_web_url = False
        self.corrupt_cells = False
        self.results_parts: dict[str, dict[str, Any]] = {}
        self.results_row_page_size: int | None = None
        self.extra_entries: list[dict[str, Any]] = []
        self.parts_page_size = 100
        self.results_page_size = 100
        self.next_part_number = 1
        self.entry_fields: dict[str, Any] = {"Temperature": 20, "Notes": "n"}
        self.entry_schema_id: str | None = "entsch_abc123"
        self.entry_page_size: int | None = None
        self.schema_page_size: int | None = None
        self.repeat_next_token = False
        self.payload_overrides: list[tuple[str, int, dict[str, Any]]] = []
        self.dropdowns = {
            "sfs_status": {
                "id": "sfs_status",
                "name": "Status",
                "options": [
                    {"id": "sfso_open", "name": "Open", "archived": False},
                    {"id": "sfso_closed", "name": "Closed", "archived": True},
                ],
            }
        }
        self.schema_catalog = {
            "entry": [
                {
                    "id": "entsch_abc123",
                    "name": "Schema entsch_abc123",
                    "modifiedAt": "2026-01-01T00:00:00Z",
                },
                {
                    "id": "entsch_two",
                    "name": "Second entry schema",
                    "modifiedAt": "2026-02-01T00:00:00Z",
                },
                {
                    "id": "entsch_three",
                    "name": "Third entry schema",
                    "modifiedAt": "2026-03-01T00:00:00Z",
                },
                {
                    "id": "entsch_four",
                    "name": "Fourth entry schema",
                    "modifiedAt": "2026-04-01T00:00:00Z",
                },
                {
                    "id": "entsch_choice",
                    "name": "Choice entry schema",
                    "modifiedAt": "2026-05-01T00:00:00Z",
                },
            ],
            "entity": [
                {
                    "id": "es_target",
                    "name": "Target entity schema",
                    "modifiedAt": "2026-01-02T00:00:00Z",
                }
            ],
            "result": [
                {
                    "id": "assaysch_abc123",
                    "name": "Schema assaysch_abc123",
                    "modifiedAt": "2026-01-02T00:00:00Z",
                },
                {
                    "id": "assaysch_two",
                    "name": "Second result schema",
                    "modifiedAt": "2026-02-02T00:00:00Z",
                },
                {
                    "id": "assaysch_three",
                    "name": "Third result schema",
                    "modifiedAt": "2026-03-02T00:00:00Z",
                },
            ],
            "run": [
                {"id": "runsch_abc123", "name": "Run schema", "modifiedAt": "2026-01-03T00:00:00Z"},
                {
                    "id": "runsch_two",
                    "name": "Second run schema",
                    "modifiedAt": "2026-02-03T00:00:00Z",
                },
            ],
        }
        self.container_schemas = [
            {
                "id": "contsch_abc123",
                "name": "Synthetic vial schema",
                "type": "container",
                "modifiedAt": "2026-01-01T00:00:00Z",
                "fieldDefinitions": [{"name": "Storage note", "type": "text"}],
            }
        ]
        self.containers = [
            {
                "id": "con_source1",
                "name": "Source vial",
                "barcode": "BAR-SOURCE",
                "modifiedAt": "2026-01-01T00:00:00Z",
                "schema": {"id": "contsch_abc123"},
                "contents": [{"entity": {"id": "ent_sample1"}}],
            },
            {
                "id": "con_dest1",
                "name": "Destination vial",
                "barcode": "BAR-DEST",
                "modifiedAt": "2026-01-02T00:00:00Z",
                "schema": {"id": "contsch_abc123"},
                "contents": [],
            },
            {
                "id": "con_batch1",
                "name": "Batch vial",
                "barcode": "BAR-BATCH",
                "modifiedAt": "2026-01-03T00:00:00Z",
                "schema": {"id": "contsch_abc123"},
                "archiveRecord": None,
                "contents": [
                    {
                        "batch": {"id": "bat_sample1", "entity": {"id": "ent_sample1"}},
                        "entity": None,
                    },
                    {"entity": {"id": "ent_registered1", "entityRegistryId": "REG-0001"}},
                ],
            },
        ]
        # Barcodes V2 GET /containers rejects with a 400 naming them in invalidBarcodes.
        self.invalid_barcodes: set[str] = set()
        self.container_page_size: int | None = None
        self.container_transfers: list[dict[str, Any]] = []
        self.field_defs = {
            "entsch_abc123": field_definitions(),
            "entsch_choice": [
                {
                    "id": "tsf_status",
                    "systemName": "status",
                    "name": "Status",
                    "__typename": "DropdownLinkFieldDefinition",
                    "derivationType": "DIRECT",
                    "linkDefinition": {"id": "sfs_status"},
                },
                {
                    "id": "tsf_status_again",
                    "systemName": "status_again",
                    "name": "Status again",
                    "__typename": "DropdownLinkFieldDefinition",
                    "derivationType": "DIRECT",
                    "linkDefinition": {"id": "sfs_status"},
                },
                {
                    "id": "tsf_orphan",
                    "systemName": "orphan",
                    "name": "Orphan",
                    "__typename": "DropdownLinkFieldDefinition",
                    "derivationType": "DIRECT",
                },
            ],
            "runsch_abc123": [
                {
                    "id": "runfield_note",
                    "systemName": "note",
                    "name": "Note",
                    "__typename": "TextFieldDefinition",
                    "derivationType": "DIRECT",
                }
            ],
            "assaysch_abc123": [
                {
                    "id": "assayfield_ct",
                    "systemName": "ct",
                    "name": "Ct",
                    "__typename": "IntegerFieldDefinition",
                    "isRequired": True,
                    "derivationType": "DIRECT",
                },
                {
                    "id": "assayfield_note",
                    "systemName": "note",
                    "name": "Note",
                    "__typename": "TextFieldDefinition",
                    "derivationType": "DIRECT",
                },
                {
                    "id": "assayfield_status",
                    "systemName": "status",
                    "name": "Status",
                    "__typename": "DropdownLinkFieldDefinition",
                    "isRequired": False,
                    "derivationType": "DIRECT",
                    "linkDefinition": {"id": "sfs_status"},
                },
                {
                    "id": "assayfield_sample",
                    "systemName": "sample",
                    "name": "Sample",
                    "__typename": "CustomEntityLinkFieldDefinition",
                    "isRequired": False,
                    "derivationType": "DIRECT",
                    "linkDefinition": {"id": "es_target"},
                },
            ],
            "es_target": [],
        }
        self.result_table_schema = "assaysch_abc123"
        self.extra_result_tables: list[dict[str, str]] = []
        self.results: list[dict[str, Any]] = [
            {"id": f"result_{n}", "entryId": ENTRY_ID, "schemaId": "assaysch_abc123"}
            for n in range(1, 4)
        ]
        self.task: dict[str, Any] = {"status": "RUNNING"}
        self.tasks: dict[str, dict[str, Any]] = {}
        self.task_responses: dict[str, list[dict[str, Any]]] = {}
        self.task_requests: list[str] = []
        self.entry_patches: list[dict[str, Any]] = []
        self.bulk_payloads: list[dict[str, Any]] = []
        self.bulk_response: dict[str, Any] = {"taskId": "task_abc123"}
        self.bulk_responses: list[dict[str, Any]] = []
        self.result_updates: list[dict[str, Any]] = []
        self.blobs: dict[str, dict[str, Any]] = {}
        self.attachments: dict[str, dict[str, Any]] = {}
        self.download_headers: list[dict[str, str]] = []
        self.assay_results: list[dict[str, Any]] = []
        self.assay_result_page_size: int | None = None
        self.linked_entities = {
            "ent_target": {"id": "ent_target", "typedSchema": {"id": "es_target"}},
            "ent_wrong_schema": {"id": "ent_wrong_schema", "typedSchema": {"id": "es_other"}},
        }

    # ---- helpers -----------------------------------------------------
    def fail(
        self, method: str, contains: str, status: int = 500, *, skip: int = 0, times: int = 1
    ) -> None:
        self.faults.append(Fault(method, contains, status, skip, times))

    def writes(self) -> list[Call]:
        return [call for call in self.calls if call.method != "GET" and call.path != "/oauth/token"]

    def part_ids(self) -> list[str]:
        return [part["id"] for part in self.parts]

    def part_text(self, part_id: str) -> str:
        part = next(item for item in self.parts if item["id"] == part_id)
        return str(part["content"]["text"])

    @property
    def version(self) -> str:
        return f"ver_{self.entry_version}"

    # ---- transport ---------------------------------------------------
    def handler(self, request: httpx.Request) -> httpx.Response:
        if request.url.path == "/oauth/token":
            return self._oauth(request)
        if request.url.host == "downloads.example.test":
            self.download_headers.append(dict(request.headers))
            identifier = request.url.path.removeprefix("/files/")
            item = self.blobs.get(identifier) or self.attachments.get(identifier)
            if item is None:
                return httpx.Response(404, request=request)
            return httpx.Response(200, stream=httpx.ByteStream(item["data"]), request=request)
        body = json.loads(request.content) if request.content else None
        requested = urlsplit(str(request.url))
        self.origin = f"{requested.scheme}://{requested.netloc}"
        call = Call(request.method, request.url.path, dict(request.url.params), body)
        self.calls.append(call)
        for index, (contains, status, payload) in enumerate(self.payload_overrides):
            if contains in call.path:
                self.payload_overrides.pop(index)
                return httpx.Response(status, json=payload, request=request)
        for fault in self.faults:
            if fault.method == call.method and fault.contains in call.path and fault.times > 0:
                if fault.skip > 0:
                    fault.skip -= 1
                    continue
                fault.times -= 1
                if fault.status == 0:
                    raise RuntimeError("simulated local failure")
                body: dict[str, Any] = {"error": {"message": "injected"}}
                if self.stamp:
                    body["note"] = self.stamp
                return httpx.Response(fault.status, json=body, request=request)
        status, payload = self._route(call)
        if self.stamp and call.method != "GET":
            payload = {**payload, "note": self.stamp}
        return httpx.Response(status, json=payload, request=request)

    def _oauth(self, request: httpx.Request) -> httpx.Response:
        form = parse_qs(request.content.decode() if request.content else "")
        self.calls.append(
            Call(
                request.method,
                request.url.path,
                {},
                {
                    "grant_type": form.get("grant_type", [""])[0],
                    "client_id": form.get("client_id", [""])[0],
                },
            )
        )
        if self.token_status >= 400:
            return httpx.Response(
                self.token_status,
                json=self.token_body or {"error": "invalid_client"},
                request=request,
            )
        return httpx.Response(
            200,
            json={
                "access_token": self.access_token,
                "token_type": "bearer",
                "expires_in": 900,
            },
            request=request,
        )

    def _route(self, call: Call) -> tuple[int, dict[str, Any]]:
        match = re.fullmatch(r"/api/v(\d)(/.*)", call.path)
        assert match, call.path
        version, path = int(match.group(1)), match.group(2)
        for pattern, name in ROUTES:
            found = re.fullmatch(pattern, path)
            if found and (f"{call.method} v{version}" in name or f"{call.method} *" in name):
                method = getattr(self, "_" + name.split("|")[-1])
                result: tuple[int, dict[str, Any]] = method(call, *found.groups())
                return result
        return 404, {"error": {"message": f"no route {call.method} {call.path}"}}

    def _list_users(self, call: Call) -> tuple[int, dict[str, Any]]:
        items = self.users
        for param, field in (("handles", "handle"), ("email.anyOf", "email"), ("ids", "id")):
            if call.params.get(param):
                values = call.params[param].split(",")
                items = [item for item in items if item.get(field) in values]
        if call.params.get("memberOf"):
            organizations = call.params["memberOf"].split(",")
            items = [item for item in items if set(organizations) <= set(item.get("memberOf", []))]
        if call.params.get("name"):
            items = [item for item in items if item.get("name") == call.params["name"]]
        if call.params.get("nameIncludes"):
            items = [item for item in items if call.params["nameIncludes"] in item.get("name", "")]
        return self._page(deepcopy(items), call, self._limited_page(call, None), key="users")

    def _get_user(self, call: Call, user_id: str) -> tuple[int, dict[str, Any]]:
        found = next((item for item in self.users if item["id"] == user_id), None)
        if found is None:
            return 404, {"error": {"message": "user not found"}}
        return 200, deepcopy(found)

    def _list_containers(self, call: Call) -> tuple[int, dict[str, Any]]:
        items = self.containers
        if call.params.get("barcodes"):
            barcodes = call.params["barcodes"].split(",")
            invalid = [barcode for barcode in barcodes if barcode in self.invalid_barcodes]
            if invalid:
                return 400, {
                    "error": {
                        "invalidBarcodes": invalid,
                        "message": "At least one provided barcode is invalid.",
                        "type": "invalid_request_error",
                    }
                }
            items = [item for item in items if item.get("barcode") in barcodes]
        if call.params.get("name"):
            items = [item for item in items if item.get("name") == call.params["name"]]
        return self._page(
            deepcopy(items),
            call,
            self._limited_page(call, self.container_page_size),
            key="containers",
        )

    def _get_container(self, call: Call, container_id: str) -> tuple[int, dict[str, Any]]:
        found = next((item for item in self.containers if item["id"] == container_id), None)
        if found is None:
            return 404, {"error": {"message": "container not found"}}
        return 200, {"container": deepcopy(found)}

    def _list_container_schemas(self, call: Call) -> tuple[int, dict[str, Any]]:
        return self._page(
            deepcopy(self.container_schemas),
            call,
            self._limited_page(call, None),
            key="containerSchemas",
        )

    def _get_container_schema(self, call: Call, schema_id: str) -> tuple[int, dict[str, Any]]:
        found = next((item for item in self.container_schemas if item["id"] == schema_id), None)
        if found is None:
            return 404, {"error": {"message": "container schema not found"}}
        return 200, deepcopy(found)

    def _create_container(self, call: Call) -> tuple[int, dict[str, Any]]:
        container = {
            "id": f"con_created{len(self.containers)}",
            "name": call.body.get("name", "Created container"),
            "barcode": call.body.get("barcode"),
            "modifiedAt": "2026-01-03T00:00:00Z",
            "schema": {"id": call.body["schemaId"]},
            "contents": [],
            **deepcopy(call.body),
        }
        self.containers.append(container)
        return 201, deepcopy(container)

    def _transfer_container(self, call: Call, destination_id: str) -> tuple[int, dict[str, Any]]:
        if not any(item["id"] == destination_id for item in self.containers):
            return 404, {"error": {"message": "destination container not found"}}
        source_id = call.body.get("sourceContainerId")
        if not any(item["id"] == source_id for item in self.containers):
            return 404, {"error": {"message": "source container not found"}}
        self.container_transfers.append(
            {"destination_id": destination_id, "body": deepcopy(call.body)}
        )
        return 200, {}

    # ---- v3 entries --------------------------------------------------
    def _entry_object(self) -> dict[str, Any]:
        entry: dict[str, Any] = {
            "id": ENTRY_ID,
            "displayId": self.display_id,
            "name": self.entry_name,
            "versionMetadata": {"versionId": self.version},
        }
        if not self.omit_entry_web_url:
            entry["webUrl"] = entry_web_url(ENTRY_ID)
        if self.entry_schema_id:
            entry["schema"] = {"id": self.entry_schema_id}
        return entry

    def _get_entry(self, call: Call, entry_id: str) -> tuple[int, dict[str, Any]]:
        if entry_id == ENTRY_ID:
            return 200, self._entry_object()
        created = self.created_entries.get(entry_id)
        if created is not None:
            result = {
                "id": created["id"],
                "name": created["name"],
                "versionMetadata": {"versionId": "ver_created1"},
                "webUrl": created["webURL"],
            }
            if created.get("schema"):
                result["schema"] = created["schema"]
            return 200, result
        return 404, {"error": {"message": "entry not found"}}

    def _get_entry_template(self, call: Call, template_id: str) -> tuple[int, dict[str, Any]]:
        template = self.entry_templates.get(template_id)
        if template is None:
            return 404, {"error": {"message": "entry template not found"}}
        return 200, deepcopy(template)

    def _get_entry_template_v2(self, call: Call, template_id: str) -> tuple[int, dict[str, Any]]:
        template = self.entry_templates.get(template_id)
        if template is None:
            return 404, {"error": {"message": "entry template not found"}}
        return 200, {"entryTemplate": deepcopy(template)}

    def _list_entry_templates(self, call: Call) -> tuple[int, dict[str, Any]]:
        templates = list(self.entry_templates.values())
        if call.params.get("name"):
            templates = [item for item in templates if item["name"] == call.params["name"]]
        return self._page(
            deepcopy(templates),
            call,
            self._limited_page(call, None),
            key="entryTemplates",
        )

    def _get_folder_v3(self, call: Call, folder_id: str) -> tuple[int, dict[str, Any]]:
        folder = self.folders.get(folder_id)
        if folder is None:
            return 404, {"error": {"message": "folder not found"}}
        return 200, deepcopy(folder)

    def _create_entry(self, call: Call) -> tuple[int, dict[str, Any]]:
        body = call.body if isinstance(call.body, dict) else {}
        template_id = body.get("entryTemplateId")
        folder_id = body.get("folderId")
        template = self.entry_templates.get(template_id)
        if template is None or folder_id not in self.folders:
            return 404, {"error": {"message": "entry template or folder not found"}}
        entry_id = f"etr_created{len(self.created_entries) + 1}"
        entry: dict[str, Any] = {
            "id": entry_id,
            "name": body["name"],
            "folderId": folder_id,
            "schema": deepcopy(template.get("schema")),
            "days": deepcopy(template.get("days") or []),
            "schemaFields": deepcopy(body.get("schemaFields") or {}),
            "webURL": entry_web_url(entry_id),
        }
        self.created_entries[entry_id] = entry
        return 201, {
            "id": entry_id,
            "name": entry["name"],
            "webUrl": entry["webURL"],
            "folder": {"id": folder_id},
            "schema": deepcopy(entry["schema"]),
        }

    def _entry_catalog(self) -> list[dict[str, Any]]:
        return [
            {
                "id": ENTRY_ID,
                "name": self.entry_name,
                "displayId": self.display_id,
                "createdAt": "2025-09-01T09:00:00Z",
                "modifiedAt": "2026-06-15T12:00:00Z",
                "schemaId": self.entry_schema_id,
                "projectId": "src_abc123",
                "folderId": "lib_abc123",
            },
            {
                "id": "etr_list2",
                "name": "Other experiment",
                "displayId": "EXP-2",
                "createdAt": "2025-12-15T00:00:00Z",
                "modifiedAt": "2026-01-01T00:00:00Z",
                "schemaId": "entsch_other",
                "projectId": "src_other",
                "folderId": "lib_other",
            },
            {
                "id": "etr_list3",
                "name": "Third note",
                "displayId": "EXP-3",
                "createdAt": "2025-11-20T00:00:00Z",
                "modifiedAt": "2025-12-01T00:00:00Z",
                "schemaId": "entsch_abc123",
                "projectId": "src_abc123",
                "folderId": "lib_other",
            },
            {
                "id": "etr_list4",
                "name": "Late note",
                "displayId": "EXP-4",
                "createdAt": "2026-03-01T00:00:00Z",
                "modifiedAt": "2026-11-01T08:30:00Z",
                "schemaId": "entsch_other",
                "projectId": "src_abc123",
                "folderId": "lib_abc123",
            },
            *self.extra_entries,
        ]

    def _modified_matches(self, modified_at: str, expression: str) -> bool:
        for part in (piece.strip() for piece in expression.split(" AND ")):
            if part.startswith(">="):
                if modified_at < part[2:].strip():
                    return False
            elif part.startswith("<="):
                if modified_at > part[2:].strip():
                    return False
            elif part.startswith(">"):
                if modified_at <= part[1:].strip():
                    return False
            elif part.startswith("<"):
                if modified_at >= part[1:].strip():
                    return False
            else:
                return False
        return True

    def _filter_entries(self, call: Call, *, version: int) -> list[dict[str, Any]]:
        rows = self._entry_catalog()
        params = call.params
        creator_key = "creatorIds" if version == 2 else "creator.anyOf"
        author_key = "authorIds.anyOf" if version == 2 else "authors.anyOf"
        if params.get(creator_key):
            ids = params[creator_key].split(",")
            rows = [row for row in rows if row.get("creatorId") in ids]
        if params.get(author_key):
            ids = set(params[author_key].split(","))
            rows = [row for row in rows if ids.intersection(row.get("authorIds", []))]
        display_ids = [item for item in params.get("displayIds", "").split(",") if item]
        if display_ids:
            rows = [row for row in rows if row["displayId"] in display_ids]
        if version == 2:
            if params.get("folderId"):
                rows = [row for row in rows if row["folderId"] == params["folderId"]]
            if params.get("projectId"):
                rows = [row for row in rows if row["projectId"] == params["projectId"]]
            if params.get("schemaId"):
                rows = [row for row in rows if row["schemaId"] == params["schemaId"]]
            names = [item.casefold() for item in params.get("names.anyOf", "").split(",") if item]
            if names:
                rows = [row for row in rows if str(row["name"]).casefold() in names]
            if params.get("name"):
                rows = [row for row in rows if row["name"] == params["name"]]
            if params.get("modifiedAt"):
                rows = [
                    row
                    for row in rows
                    if self._modified_matches(str(row["modifiedAt"]), params["modifiedAt"])
                ]
            if params.get("sort"):
                key, _, direction = params["sort"].partition(":")
                rows = sorted(
                    rows,
                    key=lambda row: str(row.get(key) or row["modifiedAt"]),
                    reverse=direction != "asc",
                )
            return rows
        # V3 entry listing does not apply folderId. Leave it unread on purpose.
        if params.get("project.eq"):
            rows = [row for row in rows if row["projectId"] == params["project.eq"]]
        if params.get("schema.eq"):
            rows = [row for row in rows if row["schemaId"] == params["schema.eq"]]
        names = [item.casefold() for item in params.get("name.anyOf", "").split(",") if item]
        if names:
            rows = [row for row in rows if str(row["name"]).casefold() in names]
        if params.get("modifiedAt.gte"):
            rows = [row for row in rows if str(row["modifiedAt"]) >= params["modifiedAt.gte"]]
        if params.get("modifiedAt.lte"):
            rows = [row for row in rows if str(row["modifiedAt"]) <= params["modifiedAt.lte"]]
        return rows

    def _limited_page(self, call: Call, cap: int | None) -> int:
        raw = call.params.get("pageSize")
        requested = int(raw) if raw else 50
        return min(requested, cap) if cap is not None else requested

    def _v3_entry(self, row: dict[str, Any]) -> dict[str, Any]:
        item: dict[str, Any] = {
            "id": row["id"],
            "name": row["name"],
            "displayId": row["displayId"],
            "modifiedAt": row["modifiedAt"],
            "folder": {"id": row["folderId"]},
            "reviewSource": {"id": row["projectId"]},
            "webUrl": entry_web_url(row["id"]),
        }
        if row["schemaId"]:
            item["schema"] = {"id": row["schemaId"]}
            item["typedSchema"] = {"id": row["schemaId"]}
        return item

    def _list_entries_v3(self, call: Call) -> tuple[int, dict[str, Any]]:
        items = [self._v3_entry(row) for row in self._filter_entries(call, version=3)]
        return self._page(items, call, self._limited_page(call, self.entry_page_size))

    def _list_entries_v2(self, call: Call) -> tuple[int, dict[str, Any]]:
        entries = []
        for row in self._filter_entries(call, version=2):
            entries.append(
                {
                    "id": row["id"],
                    "name": row["name"],
                    "displayId": row["displayId"],
                    "createdAt": _offset_stamp(str(row.get("createdAt") or row["modifiedAt"])),
                    "modifiedAt": _offset_stamp(str(row["modifiedAt"])),
                    "schemaId": row["schemaId"],
                    "projectId": row["projectId"],
                    "folderId": row["folderId"],
                    "webURL": entry_web_url(row["id"]),
                }
            )
        return self._page(
            entries, call, self._limited_page(call, self.entry_page_size), key="entries"
        )

    def _bulk_get_entries(self, call: Call) -> tuple[int, dict[str, Any]]:
        rows = self._entry_catalog()
        if call.params.get("entryIds"):
            wanted = call.params["entryIds"].split(",")
            rows = [row for row in rows if row["id"] in wanted]
        if call.params.get("displayIds"):
            wanted = call.params["displayIds"].split(",")
            rows = [row for row in rows if row["displayId"] in wanted]
        return 200, {
            "entries": [
                {
                    "id": row["id"],
                    "name": row["name"],
                    "displayId": row["displayId"],
                    "createdAt": _offset_stamp(str(row.get("createdAt") or row["modifiedAt"])),
                    "modifiedAt": _offset_stamp(str(row["modifiedAt"])),
                    "schemaId": row["schemaId"],
                    "projectId": row["projectId"],
                    "folderId": row["folderId"],
                    "webURL": entry_web_url(row["id"]),
                }
                for row in rows
            ]
        }

    def _page(
        self, items: list[Any], call: Call, size: int | None, key: str = "items"
    ) -> tuple[int, dict[str, Any]]:
        size = size or len(items) or 1
        token = call.params.get("nextToken")
        if self.repeat_next_token:
            return 200, {key: items[:size], "nextToken": token or "loop"}
        start = int(token or 0)
        end = start + size
        payload: dict[str, Any] = {key: items[start:end]}
        payload["nextToken"] = str(end) if end < len(items) else ""
        return 200, payload

    def _parts_items(self, call: Call, entry_id: str) -> tuple[int, dict[str, Any]]:
        parts = []
        for part in self.parts:
            item = deepcopy(part)
            if (
                item.get("id") == TABLE_PART
                and "versionId" not in item
                and "versionMetadata" not in item
            ):
                item["versionMetadata"] = {"versionId": self.version}
            spec = self.results_parts.get(str(item.get("id")))
            if spec is not None and item.get("__typename") == "DocumentPartResultsTable":
                # The parts list returns the documented DocumentPartResultsTable fields.
                for key, value in self._results_table_fields(str(item["id"]), spec).items():
                    item.setdefault(key, value)
            parts.append(item)
        wanted = call.params.get("id.anyOf")
        if wanted:
            parts = [part for part in parts if part["id"] in wanted.split(",")]
        return self._page(parts, call, self.parts_page_size)

    def _patch_entry(self, call: Call, entry_id: str) -> tuple[int, dict[str, Any]]:
        body = call.body
        self.entry_patches.append(body)
        if "schemaFields" in body:
            definitions = self.field_defs["entsch_abc123"]
            for key, wrapper in body["schemaFields"].items():
                matches = [item for item in definitions if item.get("id") == key]
                if not matches:
                    return 400, {"error": {"message": f"schemaFields: Invalid field: {key}"}}
                self.entry_fields[matches[0]["name"]] = wrapper["value"]
            return 200, {"id": entry_id}
        mutation = body["partsMutation"]
        if mutation.get("version") != self.version:
            return 409, {
                "error": {
                    "message": (
                        "Version mismatch, the document has been modified since you last read it. "
                        f"Provided version: {mutation.get('version')!r}, "
                        f"Current document version: {self.version!r}."
                    )
                }
            }
        kind = mutation["operationType"]
        if kind == "insert":
            self._insert(mutation["position"], mutation["parts"])
        elif kind == "delete":
            src = mutation["sourceRange"]
            first = self.part_ids().index(src["start"])
            last = self.part_ids().index(src["end"])
            del self.parts[first : last + 1]
        elif kind == "move":
            src = mutation["sourceRange"]
            first = self.part_ids().index(src["start"])
            last = self.part_ids().index(src["end"])
            moved = self.parts[first : last + 1]
            del self.parts[first : last + 1]
            self._insert_existing(mutation["destinationPosition"], moved)
        elif kind == "update_table_cells":
            self._update_table_cells(mutation)
        elif kind == "insert_table_rows":
            self._insert_table_rows(mutation)
        else:
            return 400, {"error": {"message": f"unknown operation {kind}"}}
        self.entry_version += 1
        return 200, {"id": entry_id}

    def _insert(self, position: str, new_parts: list[dict[str, Any]]) -> None:
        created = []
        for part in new_parts:
            part_id = f"docpart_new{self.next_part_number}"
            self.next_part_number += 1
            content = {key: value for key, value in part.items() if key != "contentType"}
            content["__typename"] = "TextDocumentContent"
            created.append({"id": part_id, "content": content})
        self._insert_existing(position, created)

    def _insert_existing(self, position: str, parts: list[dict[str, Any]]) -> None:
        if position == "beginning_of_document":
            index = 0
        elif position == "end_of_document":
            index = len(self.parts)
        else:
            # A part id inserts immediately before that part.
            index = self.part_ids().index(position)
        self.parts[index:index] = parts

    # ---- tables ------------------------------------------------------
    def _table_rows_url(self, collection: str, part_id: str) -> str:
        return f"{self.origin}/api/v3/{collection}/{part_id}/rows/items"

    def _get_table(self, call: Call, part_id: str) -> tuple[int, dict[str, Any]]:
        if part_id != TABLE_PART:
            return 404, {"error": {"message": "table not found"}}
        rows = self.table_content["rows"]
        row_count = self.row_count_override
        if row_count is None:
            row_count = len(rows) if isinstance(rows, list) else None
        payload: dict[str, Any] = {
            "id": part_id,
            "__typename": "DocumentPartUnstructuredTable",
            "name": self.table_content.get("name"),
            "columnDefinitions": deepcopy(self.table_content.get("columnDefinitions")),
            "isEvaluated": self.table_content.get("isEvaluated"),
            "rowCount": row_count,
            "rows": (
                self.rows_field
                if self.rows_field is not None
                else self._table_rows_url("document-part-unstructured-table", part_id)
            ),
        }
        if "caption" in self.table_content:
            payload["caption"] = self.table_content["caption"]
        if not self.omit_table_version:
            payload["versionMetadata"] = {"versionId": self.version}
        return 200, payload

    def _table_rows(self, call: Call, part_id: str) -> tuple[int, dict[str, Any]]:
        if part_id != TABLE_PART:
            return 404, {"error": {"message": "table not found"}}
        rows = self.table_content["rows"]
        if not isinstance(rows, list):
            return 200, {"items": rows, "nextToken": ""}
        return self._page_row_items(rows, call, page_cap=self.table_page_size)

    def _get_results_table(self, call: Call, part_id: str) -> tuple[int, dict[str, Any]]:
        spec = self.results_parts.get(part_id)
        if spec is None:
            return 404, {"error": {"message": "results table not found"}}
        return 200, {
            "id": part_id,
            "__typename": "DocumentPartResultsTable",
            **self._results_table_fields(part_id, spec),
            "versionMetadata": {"versionId": self.version},
        }

    def _results_table_fields(self, part_id: str, spec: dict[str, Any]) -> dict[str, Any]:
        """`name`, `columnDefinitions`, `resultSchema`, `submissionStatus`, `rowCount`, `rows`."""
        return {
            "name": spec.get("name"),
            "columnDefinitions": deepcopy(spec.get("columnDefinitions") or []),
            "isEvaluated": spec.get("isEvaluated", True),
            "resultSchema": deepcopy(spec.get("resultSchema")),
            "submissionStatus": spec.get("submissionStatus", "UNSUBMITTED"),
            "rowCount": spec.get("row_count", len(spec.get("rows") or [])),
            "rows": self._table_rows_url("document-part-results-table", part_id),
        }

    def _results_rows(self, call: Call, part_id: str) -> tuple[int, dict[str, Any]]:
        spec = self.results_parts.get(part_id)
        if spec is None:
            return 404, {"error": {"message": "results table not found"}}
        return self._page_row_items(
            spec.get("rows") or [], call, page_cap=self.results_row_page_size
        )

    def add_plate_reader_table(self) -> str:
        """Attach the synthetic plate-reader table used by the examples/ results fixtures."""
        self.field_defs["assaysch_plate"] = [
            {
                "id": "assayfield_well",
                "systemName": "well",
                "name": "Well",
                "__typename": "TextFieldDefinition",
                "isRequired": True,
                "derivationType": "DIRECT",
            },
            {
                "id": "assayfield_absorbance",
                "systemName": "absorbance",
                "name": "Absorbance",
                "__typename": "FloatFieldDefinition",
                "isRequired": True,
                "derivationType": "DIRECT",
            },
            {
                "id": "assayfield_wavelength",
                "systemName": "wavelength_nm",
                "name": "Wavelength (nm)",
                "__typename": "IntegerFieldDefinition",
                "isRequired": False,
                "derivationType": "DIRECT",
            },
        ]
        self.extra_result_tables.append(
            {
                "type": "results_table",
                "apiId": "strtbl_plate",
                "name": "Plate reader",
                "assayResultSchemaId": "assaysch_plate",
                "columns": ["well", "absorbance", "wavelength_nm"],
            }
        )
        return "strtbl_plate"

    def add_evaluated_results_table(self, part_id: str = "docpart_rt1") -> str:
        """A results table evaluated in the web app, with link columns and empty fields.

        Row 0 follows the "Working with Tables" example response for
        `GET /document-part-results-table/{id}/rows/items`, with synthetic IDs.
        """
        self.results_parts[part_id] = {
            "name": "Evaluated links",
            "columnDefinitions": [
                {
                    "__typename": "SchemaFieldColumnDefinition",
                    "name": "Sample",
                    "columnId": "col_s",
                },
                {"__typename": "SchemaFieldColumnDefinition", "name": "Box", "columnId": "col_b"},
                {"__typename": "SchemaFieldColumnDefinition", "name": "Notes", "columnId": "col_n"},
            ],
            "rows": evaluated_results_rows(),
            "resultSchema": {"id": "assaysch_links"},
            "submissionStatus": "SUBMITTED",
            "isEvaluated": True,
        }
        return part_id

    def add_panel_tables(self) -> list[str]:
        """One run written across numbered results tables of one schema, as an SDK app does.

        "Panel Testing 1" to "Panel Testing 3" share schema ``assaysch_panel``: a text
        ``Processing run`` column and a ``Sample`` container link. "Panel Testing 4" holds
        only the empty ``UNSUBMITTED`` placeholder row, and "Panel Testing notes" uses
        another schema with no run column. All IDs are synthetic.
        """
        self.field_defs["assaysch_panel"] = [
            {
                "id": "assayfield_run",
                "systemName": "processing_run_id",
                "name": "Processing run",
                "__typename": "TextFieldDefinition",
            },
            {
                "id": "assayfield_sample",
                "systemName": "sample",
                "name": "Sample",
                "__typename": "StorableLinkFieldDefinition",
            },
        ]
        self.field_defs["assaysch_note"] = [
            {
                "id": "assayfield_comment",
                "systemName": "comment",
                "name": "Comment",
                "__typename": "TextFieldDefinition",
            }
        ]
        columns = [
            {
                "__typename": "SchemaFieldColumnDefinition",
                "name": "Processing run",
                "columnId": "col_run",
                "fieldDefinitionApiId": "assayfield_run",
            },
            {
                "__typename": "SchemaFieldColumnDefinition",
                "name": "Sample",
                "columnId": "col_sample",
                "fieldDefinitionApiId": "assayfield_sample",
            },
        ]

        def row(number: int, run: str, container: str) -> dict[str, Any]:
            return {
                "id": f"strow_p{number}",
                "rowId": f"row_p{number}",
                "submissionStatus": "SUBMITTED",
                "cells": [
                    {"inputValue": {"__typename": "TextValue", "value": run}},
                    {
                        "inputValue": {"__typename": "ObjectLinkValue", "value": container},
                        "evaluationValue": {
                            "__typename": "ObjectLinkCellEvaluationValue",
                            "id": container,
                            "name": f"Tube {container}",
                        },
                    },
                ],
            }

        tables: dict[str, tuple[str, list[dict[str, Any]], str]] = {
            "1": (
                "assaysch_panel",
                [row(1, "run_a", "con_tube1"), row(2, "run_a", "con_tube2")],
                "SUBMITTED",
            ),
            "2": (
                "assaysch_panel",
                [row(3, "run_a", "con_tube1"), row(4, "run_b", "con_tube3")],
                "SUBMITTED",
            ),
            "3": ("assaysch_panel", [row(5, "run_b", "con_tube3")], "SUBMITTED"),
            "4": (
                "assaysch_panel",
                [{"id": "strow_p0", "submissionStatus": "UNSUBMITTED", "cells": [{}, {}]}],
                "UNSUBMITTED",
            ),
            "notes": (
                "assaysch_note",
                [
                    {
                        "id": "strow_n1",
                        "submissionStatus": "SUBMITTED",
                        "cells": [{"inputValue": {"__typename": "TextValue", "value": "run_a"}}],
                    }
                ],
                "SUBMITTED",
            ),
        }
        part_ids = []
        for suffix, (schema_id, rows, status) in tables.items():
            part_id = f"docpart_panel{suffix}"
            name = f"Panel Testing {suffix}"
            self.results_parts[part_id] = {
                "name": name,
                "columnDefinitions": deepcopy(
                    columns
                    if schema_id == "assaysch_panel"
                    else [
                        {
                            "__typename": "SchemaFieldColumnDefinition",
                            "name": "Comment",
                            "columnId": "col_comment",
                            "fieldDefinitionApiId": "assayfield_comment",
                        }
                    ]
                ),
                "rows": rows,
                "resultSchema": {"__typename": "ResultSchemaRef", "id": schema_id},
                "submissionStatus": status,
                "isEvaluated": True,
            }
            self.parts.append(
                {
                    "id": part_id,
                    "__typename": "DocumentPartResultsTable",
                    "name": name,
                    "resultSchema": {"__typename": "ResultSchemaRef", "id": schema_id},
                }
            )
            self.extra_result_tables.append(
                {
                    "type": "results_table",
                    "apiId": f"strtbl_panel{suffix}",
                    "name": name,
                    "assayResultSchemaId": schema_id,
                    "columns": [item["systemName"] for item in self.field_defs[schema_id]],
                }
            )
            part_ids.append(part_id)
        return part_ids

    def add_example_results_part(
        self,
        part_id: str = "docpart_example",
        *,
        rows: list[dict[str, Any]] | None = None,
        row_count: int | None = None,
        submission_status: str = "SUBMITTED",
    ) -> str:
        """Add the V3 results part that matches the V2 ``strtbl_example`` table.

        ``row_count`` sets the documented ``rowCount`` without building that many rows.
        """
        self.parts.append(
            {
                "id": part_id,
                "__typename": "DocumentPartResultsTable",
                "name": "Assay results",
                "resultSchema": {"__typename": "ResultSchemaRef", "id": self.result_table_schema},
            }
        )
        self.results_parts[part_id] = {
            "name": "Assay results",
            "columnDefinitions": [
                {"__typename": "SchemaFieldColumnDefinition", "name": "Ct", "columnId": "col_ct"}
            ],
            "rows": rows if rows is not None else default_results_rows(),
            "resultSchema": {"id": self.result_table_schema},
            "submissionStatus": submission_status,
            "isEvaluated": True,
        }
        if row_count is not None:
            self.results_parts[part_id]["row_count"] = row_count
        return part_id

    def add_results_table(self, part_id: str = "docpart_rt1") -> str:
        """Install a synthetic results table the rows endpoint can page."""
        rows = default_results_rows()
        self.results_parts[part_id] = {
            "name": "qPCR, plate 1",
            "columnDefinitions": [
                {"__typename": "SchemaFieldColumnDefinition", "name": "Ct", "columnId": "col_ct"}
            ],
            "rows": rows,
            "resultSchema": {"id": "assaysch_abc123"},
            "submissionStatus": "HAS_UNSUBMITTED_CHANGES",
            "isEvaluated": True,
        }
        return part_id

    def _page_row_items(
        self, rows: list[dict[str, Any]], call: Call, *, page_cap: int | None
    ) -> tuple[int, dict[str, Any]]:
        requested = int(call.params.get("pageSize") or "50")
        if requested > 100:
            return 400, {"error": {"message": "pageSize must be at most 100"}}
        size = page_cap or requested
        api_rows = [self._api_row(index, row) for index, row in enumerate(rows)]
        if self.repeat_next_token:
            incoming = call.params.get("nextToken")
            return 200, {"items": api_rows[:size], "nextToken": incoming or "loop"}
        raw_token = call.params.get("nextToken") or "0"
        try:
            start = int(raw_token)
        except ValueError:
            # A non-numeric token is still a page, so a client can notice it repeating.
            start = 0
        end = start + size
        return 200, {
            "items": api_rows[start:end],
            "nextToken": str(end) if end < len(rows) else "",
        }

    def _api_row(self, index: int, row: dict[str, Any]) -> dict[str, Any]:
        cells = []
        for cell in row.get("cells") or []:
            if self.corrupt_cells:
                cells.append({"inputValue": "bad"})
                continue
            input_value = cell.get("inputValue") or {}
            if isinstance(input_value, dict) and "__typename" in input_value:
                api_input = deepcopy(input_value)
            else:
                api_input = {"__typename": "TextValue", "value": input_value.get("value", "")}
            api_cell: dict[str, Any] = {
                "inputValue": api_input,
                "evaluationValue": None,
                "format": cell.get("format"),
                "mergeRange": cell.get("mergeRange"),
                "mergeRangeParent": cell.get("mergeRangeParent"),
                "selectorId": cell.get("selectorId"),
            }
            evaluated = cell.get("evaluatedValue") or cell.get("evaluationValue")
            if isinstance(evaluated, dict) and "__typename" in evaluated:
                api_cell["evaluationValue"] = deepcopy(evaluated)
            elif isinstance(evaluated, dict) and evaluated.get("value") is not None:
                api_cell["evaluationValue"] = {
                    "__typename": "TextValue",
                    "value": evaluated.get("value"),
                }
            if "errors" in cell:
                api_cell["errors"] = deepcopy(cell["errors"])
            cells.append(api_cell)
        api_row: dict[str, Any] = {
            "id": row.get("id") or f"utrow_{index}",
            "rowIndex": index,
            "cells": cells,
        }
        for key in ("rowId", "submissionStatus", "isArchived", "isSubmittable", "result"):
            if row.get(key) is not None:
                api_row[key] = deepcopy(row[key])
        return api_row

    def _update_table_cells(self, mutation: dict[str, Any]) -> None:
        if mutation.get("documentPartId") != TABLE_PART:
            raise KeyError(mutation.get("documentPartId"))
        rows = self.table_content["rows"]
        for update in mutation["cellUpdates"]:
            cell = rows[update["rowIndex"]]["cells"][update["columnIndex"]]
            cell["inputValue"] = {
                "type": "text_value",
                "value": update["inputValue"]["value"],
            }
            cell.pop("evaluatedValue", None)
            cell.pop("evaluationValue", None)
        self.table_content["isEvaluated"] = False

    def _insert_table_rows(self, mutation: dict[str, Any]) -> None:
        if mutation.get("documentPartId") != TABLE_PART:
            raise KeyError(mutation.get("documentPartId"))
        for row in mutation["rows"]:
            ordered = sorted(row["cells"], key=lambda item: item["columnIndex"])
            self.table_content["rows"].append(
                {
                    "cells": [
                        {
                            "inputValue": {
                                "type": "text_value",
                                "value": cell["inputValue"]["value"],
                            }
                        }
                        for cell in ordered
                    ]
                }
            )
        self.table_content["isEvaluated"] = False

    # ---- schemas -----------------------------------------------------
    def _schema(self, call: Call, schema_id: str) -> tuple[int, dict[str, Any]]:
        if schema_id not in self.field_defs:
            return 404, {"error": {"message": "schema not found"}}
        return 200, {"id": schema_id, "name": f"Schema {schema_id}"}

    def _schema_fields(self, call: Call, schema_id: str) -> tuple[int, dict[str, Any]]:
        return self._page(deepcopy(self.field_defs[schema_id]), call, 5)

    def _list_kind(self, call: Call, kind: str) -> tuple[int, dict[str, Any]]:
        schemas = deepcopy(self.schema_catalog[kind])
        name = call.params.get("name.anyOf.caseSensitive")
        if name is not None:
            schemas = [schema for schema in schemas if schema.get("name") == name]
        return self._page(
            schemas,
            call,
            self._limited_page(call, self.schema_page_size),
        )

    def _list_entry_schemas(self, call: Call) -> tuple[int, dict[str, Any]]:
        return self._list_kind(call, "entry")

    def _list_result_schemas(self, call: Call) -> tuple[int, dict[str, Any]]:
        return self._list_kind(call, "result")

    def _list_run_schemas(self, call: Call) -> tuple[int, dict[str, Any]]:
        return self._list_kind(call, "run")

    def _list_entity_schemas(self, call: Call) -> tuple[int, dict[str, Any]]:
        return self._list_kind(call, "entity")

    def _get_linked_entity(self, call: Call, entity_id: str) -> tuple[int, dict[str, Any]]:
        entity = self.linked_entities.get(entity_id)
        if entity is None:
            return 404, {"error": {"message": "entity not found"}}
        return 200, deepcopy(entity)

    def _dropdown(self, call: Call, dropdown_id: str) -> tuple[int, dict[str, Any]]:
        found = self.dropdowns.get(dropdown_id)
        if found is None:
            return 404, {"error": {"message": "dropdown not found"}}
        return 200, {"id": found["id"], "name": found["name"]}

    def _dropdown_options(self, call: Call, dropdown_id: str) -> tuple[int, dict[str, Any]]:
        found = self.dropdowns.get(dropdown_id)
        if found is None:
            return 404, {"error": {"message": "dropdown not found"}}
        return 200, {"items": deepcopy(found["options"])}

    # ---- v2 entry / project / task -----------------------------------
    def _get_entry_v2(self, call: Call, entry_id: str) -> tuple[int, dict[str, Any]]:
        created = self.created_entries.get(entry_id)
        if created is not None:
            return 200, {"entry": deepcopy(created)}
        result_tables = [
            {
                "type": "results_table",
                "apiId": "strtbl_example",
                "name": "Assay results",
                "assayResultSchemaId": self.result_table_schema,
                "columns": ["ct"],
            },
            *deepcopy(self.extra_result_tables),
        ]
        entry: dict[str, Any] = {
            "id": ENTRY_ID,
            "folderId": "lib_abc123",
            "modifiedAt": "2026-01-01T00:00:00Z",
            "fields": dict(self.entry_fields),
            "days": [{"notes": [*result_tables, {"type": "text", "text": "ignored"}]}],
        }
        if not self.omit_entry_web_url:
            entry["webURL"] = entry_web_url(ENTRY_ID)
        if self.entry_schema_id:
            entry["schema"] = {"id": self.entry_schema_id}
        return 200, {"entry": entry}

    def _get_folder(self, call: Call, folder_id: str) -> tuple[int, dict[str, Any]]:
        return 200, {"folder": {"id": folder_id, "projectId": "src_abc123"}}

    def _get_project(self, call: Call, project_id: str) -> tuple[int, dict[str, Any]]:
        if project_id != "src_abc123":
            return 404, {"error": {"message": "project not found"}}
        return 200, {"project": {"id": project_id, "name": "Synthetic project"}}

    def _get_task(self, call: Call, task_id: str) -> tuple[int, dict[str, Any]]:
        self.task_requests.append(task_id)
        if self.task_responses.get(task_id):
            return 200, self.task_responses[task_id].pop(0)
        return 200, dict(self.tasks.get(task_id, self.task))

    def _bulk_create(self, call: Call) -> tuple[int, dict[str, Any]]:
        self.bulk_payloads.append(call.body)
        if self.bulk_responses:
            return 202, self.bulk_responses.pop(0)
        return 202, dict(self.bulk_response)

    # ---- blobs -------------------------------------------------------
    def _new_blob(self, body: dict[str, Any]) -> dict[str, Any]:
        blob_id = f"00000000-0000-4000-8000-{len(self.blobs) + 1:012d}"
        blob = {
            "id": blob_id,
            "name": body.get("name"),
            "type": body.get("type"),
            "mimeType": body.get("mimeType", "application/octet-stream"),
            "uploadStatus": "IN_PROGRESS",
            "data": b"",
            "parts": {},
        }
        self.blobs[blob_id] = blob
        return blob

    @staticmethod
    def _blob_view(blob: dict[str, Any]) -> dict[str, Any]:
        return {key: blob[key] for key in ("id", "name", "type", "mimeType", "uploadStatus")}

    @staticmethod
    def _decoded(body: dict[str, Any]) -> bytes | None:
        data = base64.b64decode(body.get("data64", ""), validate=True)
        if hashlib.md5(data, usedforsecurity=False).hexdigest() != body.get("md5"):
            return None
        return data

    def _create_blob(self, call: Call) -> tuple[int, dict[str, Any]]:
        data = self._decoded(call.body)
        if data is None:
            return 400, {"error": {"message": "md5 does not match data64"}}
        blob = self._new_blob(call.body)
        blob["data"] = data
        blob["uploadStatus"] = "COMPLETE"
        return 200, self._blob_view(blob)

    def _get_blob(self, call: Call, blob_id: str) -> tuple[int, dict[str, Any]]:
        blob = self.blobs.get(blob_id)
        if blob is None:
            return 404, {"error": {"message": "blob not found"}}
        return 200, self._blob_view(blob)

    def _blob_download_url(self, call: Call, blob_id: str) -> tuple[int, dict[str, Any]]:
        if blob_id not in self.blobs:
            return 404, {"error": {"message": "blob not found"}}
        return 200, {"downloadURL": f"https://downloads.example.test/files/{blob_id}"}

    def _get_attachment(self, call: Call, attachment_id: str) -> tuple[int, dict[str, Any]]:
        attachment = self.attachments.get(attachment_id)
        if attachment is None:
            return 404, {"error": {"message": "attachment not found"}}
        return 200, {key: value for key, value in attachment.items() if key != "data"}

    def _start_blob(self, call: Call) -> tuple[int, dict[str, Any]]:
        return 200, self._blob_view(self._new_blob(call.body))

    def _blob_part(self, call: Call, blob_id: str) -> tuple[int, dict[str, Any]]:
        blob = self.blobs.get(blob_id)
        if blob is None or blob["uploadStatus"] != "IN_PROGRESS":
            return 400, {"error": {"message": "blob is not accepting parts"}}
        data = self._decoded(call.body)
        if data is None:
            return 400, {"error": {"message": "md5 does not match data64"}}
        number = call.body["partNumber"]
        blob["parts"][number] = data
        etag = '"' + hashlib.md5(data, usedforsecurity=False).hexdigest() + '"'
        return 200, {"partNumber": number, "eTag": etag}

    def _complete_blob(self, call: Call, blob_id: str) -> tuple[int, dict[str, Any]]:
        blob = self.blobs.get(blob_id)
        if blob is None or blob["uploadStatus"] != "IN_PROGRESS":
            return 400, {"error": {"message": "blob is not in progress"}}
        chunks = []
        for part in sorted(call.body.get("parts", []), key=lambda item: item["partNumber"]):
            data = blob["parts"].get(part["partNumber"])
            expected = '"' + hashlib.md5(data or b"", usedforsecurity=False).hexdigest() + '"'
            if data is None or part.get("eTag") != expected:
                return 400, {"error": {"message": "unknown part or eTag"}}
            chunks.append(data)
        blob["data"] = b"".join(chunks)
        blob["uploadStatus"] = "COMPLETE"
        return 200, self._blob_view(blob)

    def _abort_blob(self, call: Call, blob_id: str) -> tuple[int, dict[str, Any]]:
        blob = self.blobs.get(blob_id)
        if blob is None:
            return 404, {"error": {"message": "blob not found"}}
        blob["uploadStatus"] = "ABORTED"
        return 200, {}

    def complete_upload(
        self, task_id: str = "task_abc123", *, payload_index: int = -1
    ) -> list[str]:
        """Create pending V2 results from a bulk-create payload and finish its task."""
        payload = self.bulk_payloads[payload_index]
        created = []
        for record in payload["assayResults"]:
            number = len(self.assay_results) + len(created) + 1
            created.append(
                {
                    "id": f"00000000-0000-4000-8000-{number:012d}",
                    "v3Id": f"result_up{number}",
                    "entryId": None,
                    "isReviewed": False,
                    "archiveRecord": None,
                    "createdAt": f"2026-11-19T00:00:{number:02d}Z",
                    "schema": {"id": record["schemaId"], "name": "Synthetic result schema"},
                    "projectId": record.get("projectId"),
                    "fields": {
                        name: {"value": value.get("value"), "displayValue": str(value.get("value"))}
                        for name, value in record["fields"].items()
                    },
                }
            )
        self.assay_results.extend(created)
        self.tasks[task_id] = {
            "status": "SUCCEEDED",
            "response": {
                "assayResults": [{"id": item["id"], "v3Id": item["v3Id"]} for item in created]
            },
        }
        return [item["id"] for item in created]

    def accept_results(self, result_ids: list[str], entry_id: str = ENTRY_ID) -> None:
        for item in self.assay_results:
            if item["id"] in result_ids:
                item["entryId"] = entry_id
                item["isReviewed"] = True

    def _bulk_get_assay_results(self, call: Call) -> tuple[int, dict[str, Any]]:
        requested = [item for item in call.params.get("assayResultIds", "").split(",") if item]
        if not requested or len(requested) > 200:
            return 400, {"error": {"message": "assayResultIds must list 1 to 200 IDs"}}
        if any(item.startswith("result_") for item in requested):
            return 400, {"error": {"message": "One or more provided assayResultIds were invalid"}}
        found = [deepcopy(item) for item in self.assay_results if item["id"] in requested]
        return 200, {"assayResults": found}

    def _list_assay_results_v2(self, call: Call) -> tuple[int, dict[str, Any]]:
        if "entry.eq" in call.params or "entryId" in call.params:
            return 400, {"error": {"message": "unsupported filter"}}
        items = self.assay_results
        if call.params.get("archiveReason") != "ANY_ARCHIVED_OR_NOT_ARCHIVED":
            items = [r for r in items if not r.get("archiveRecord")]
        if "schemaId" in call.params:
            items = [
                r for r in items if (r.get("schema") or {}).get("id") == call.params["schemaId"]
            ]
        if "createdAt.gte" in call.params:
            items = [r for r in items if r["createdAt"] >= call.params["createdAt.gte"]]
        if "createdAt.lte" in call.params:
            items = [r for r in items if r["createdAt"] <= call.params["createdAt.lte"]]
        size = self.assay_result_page_size or int(call.params.get("pageSize") or 50)
        return self._page(deepcopy(items), call, size, key="assayResults")

    # ---- results -----------------------------------------------------
    def _v3_results(self) -> list[dict[str, Any]]:
        """V3 results: ``self.results`` plus the V3 view of each V2 result with a ``v3Id``."""
        return [*self.results, *(_v3_view(item) for item in self.assay_results if item.get("v3Id"))]

    def _list_results(self, call: Call) -> tuple[int, dict[str, Any]]:
        items = self._v3_results()
        if "id.anyOf" in call.params:
            wanted = call.params["id.anyOf"].split(",")
            items = [r for r in items if r["id"] in wanted]
        if "entry.eq" in call.params:
            items = [r for r in items if _result_entry_id(r) == call.params["entry.eq"]]
        if "schema.eq" in call.params:
            items = [r for r in items if _result_schema_id(r) == call.params["schema.eq"]]
        return self._page(deepcopy(items), call, self.results_page_size)

    def _get_result(self, call: Call, result_id: str) -> tuple[int, dict[str, Any]]:
        for item in self._v3_results():
            if item["id"] == result_id:
                return 200, deepcopy(item)
        return 404, {"error": {"message": "result not found"}}

    def _patch_result(self, call: Call, result_id: str) -> tuple[int, dict[str, Any]]:
        body = call.body if isinstance(call.body, dict) else {}
        for item in self.results:
            if item["id"] != result_id:
                continue
            fields = body.get("schemaFields")
            if isinstance(fields, dict):
                existing = item.get("schemaFields")
                by_id: dict[str, dict[str, Any]] = {}
                if isinstance(existing, list):
                    for entry in existing:
                        if not isinstance(entry, dict):
                            continue
                        definition = entry.get("fieldDefinition")
                        field_id = definition.get("id") if isinstance(definition, dict) else None
                        if isinstance(field_id, str):
                            by_id[field_id] = entry
                for field_id, payload in fields.items():
                    value = payload.get("value") if isinstance(payload, dict) else payload
                    by_id[str(field_id)] = {
                        "__typename": "SchemaFieldValue",
                        "fieldDefinition": {
                            "id": str(field_id),
                            "__typename": "SchemaFieldDefinitionRef",
                        },
                        "value": value,
                    }
                item["schemaFields"] = list(by_id.values())
            item["modifiedAt"] = "2026-11-19T00:00:00Z"
            self.result_updates.append({"id": result_id, "body": deepcopy(body)})
            return 200, deepcopy(item)
        return 404, {"error": {"message": "result not found"}}


ROUTES: list[tuple[str, str]] = [
    (r"/users", "GET v2|list_users"),
    (r"/users/(\w+)", "GET v2|get_user"),
    (r"/container-schemas", "GET v2|list_container_schemas"),
    (r"/container-schemas/(\w+)", "GET v2|get_container_schema"),
    (r"/containers", "GET v2|list_containers"),
    (r"/containers", "POST v2|create_container"),
    (r"/containers/(\w+):transfer", "POST v2|transfer_container"),
    (r"/containers/(\w+)", "GET v2|get_container"),
    (r"/entry/items", "GET v3|list_entries_v3"),
    (r"/entry-template/(\w+)", "GET v3|get_entry_template"),
    (r"/folder/(\w+)", "GET v3|get_folder_v3"),
    (r"/entry", "POST v3|create_entry"),
    (r"/entry-templates", "GET v2|list_entry_templates"),
    (r"/entry-templates/(\w+)", "GET v2|get_entry_template_v2"),
    (r"/entry-templates/(\w+)", "GET v3|get_entry_template"),
    (r"/entries:bulk-get", "GET v2|bulk_get_entries"),
    (r"/entries", "GET v2|list_entries_v2"),
    (r"/entry/(etr_\w+)", "GET v3|get_entry"),
    (r"/entry/(etr_\w+)", "PATCH v3|patch_entry"),
    (r"/entry/(etr_\w+)/parts/items", "GET v3|parts_items"),
    (r"/document-part-unstructured-table/(\w+)/rows/items", "GET v3|table_rows"),
    (r"/document-part-unstructured-table/(\w+)", "GET v3|get_table"),
    (r"/document-part-results-table/(\w+)/rows/items", "GET v3|results_rows"),
    (r"/document-part-results-table/(\w+)", "GET v3|get_results_table"),
    (r"/entry-schema/items", "GET v3|list_entry_schemas"),
    (r"/result-schema/items", "GET v3|list_result_schemas"),
    (r"/run-schema/items", "GET v3|list_run_schemas"),
    (r"/entity-schema/items", "GET v3|list_entity_schemas"),
    (r"/(?:entry|entity|result|run)-schema/(\w+)/field-definitions/items", "GET v3|schema_fields"),
    (r"/(?:entry|entity|result|run)-schema/(\w+)", "GET v3|schema"),
    (r"/dropdown/(\w+)/options/items", "GET v3|dropdown_options"),
    (r"/dropdown/(\w+)", "GET v3|dropdown"),
    (r"/custom-entity/(ent_\w+)", "GET v3|get_linked_entity"),
    (r"/dna-sequence/(seq_\w+)", "GET v3|get_linked_entity"),
    (r"/aa-sequence/(aaseq_\w+)", "GET v3|get_linked_entity"),
    (r"/result/items", "GET v3|list_results"),
    (r"/result/(\w+)", "PATCH v3|patch_result"),
    (r"/result/(\w+)", "GET v3|get_result"),
    (r"/entries/(\w+)", "GET v2|get_entry_v2"),
    (r"/folders/(\w+)", "GET v2|get_folder"),
    (r"/projects/(\w+)", "GET v2|get_project"),
    (r"/tasks/(\w+)", "GET v2|get_task"),
    (r"/assay-results:bulk-create", "POST v2|bulk_create"),
    (r"/blobs", "POST v2|create_blob"),
    (r"/blobs:start-multipart-upload", "POST v2|start_blob"),
    (r"/blobs/([\w-]+)", "GET v2|get_blob"),
    (r"/blobs/([\w-]+)/download-url", "GET v2|blob_download_url"),
    (r"/attachment/(attachment_\w+)", "GET v3|get_attachment"),
    (r"/blobs/([\w-]+)/parts", "POST v2|blob_part"),
    (r"/blobs/([\w-]+):complete-upload", "POST v2|complete_blob"),
    (r"/blobs/([\w-]+):abort-upload", "POST v2|abort_blob"),
    (r"/assay-results:bulk-get", "GET v2|bulk_get_assay_results"),
    (r"/assay-results", "GET v2|list_assay_results_v2"),
]
