"""Every JSON body the client sends must satisfy the vendored OpenAPI request schema."""

from __future__ import annotations

import hashlib
import json
import re
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml
from fake_benchling import ENTRY_ID, TABLE_PART, TEXT_PART
from jsonschema import Draft202012Validator
from referencing import Registry, Resource
from referencing.jsonschema import DRAFT202012

from benchling_tools.listing import ENTRY_SORTS, EntrySort
from benchling_tools.plans import blobs

ROOT = Path(__file__).resolve().parents[1]
MANIFEST = json.loads((ROOT / "contracts" / "openapi-manifest.json").read_text())

_V3_ENTRY = re.compile(r"/api/v3/entry/[^/]+$")
_V3_ENTRY_CREATE = re.compile(r"/api/v3/entry$")
_V3_RESULT = re.compile(r"/api/v3/result/[^/]+$")
_V2_BULK = re.compile(r"/api/v2/assay-results:bulk-create$")
_V2_BLOB_ROUTES = (
    (re.compile(r"/api/v2/blobs$"), "BlobCreate"),
    (re.compile(r"/api/v2/blobs:start-multipart-upload$"), "BlobMultipartCreate"),
    (re.compile(r"/api/v2/blobs/[^/]+/parts$"), "BlobPartCreate"),
    (re.compile(r"/api/v2/blobs/[^/]+:complete-upload$"), "BlobComplete"),
)


def _contract(name: str) -> dict[str, Any]:
    matches = [item for item in MANIFEST["contracts"] if item["name"] == name]
    if len(matches) != 1:
        raise AssertionError(f"manifest is missing {name}")
    return matches[0]


def test_vendored_specs_match_the_manifest_checksums() -> None:
    for name in ("benchling-v3", "benchling-v2"):
        contract = _contract(name)
        snapshot = ROOT / str(contract["snapshot"])
        digest = hashlib.sha256(snapshot.read_bytes()).hexdigest()
        assert digest == contract["sha256"]
        assert contract["captured_at"] == "2026-11-19"


@lru_cache(maxsize=4)
def _spec(name: str) -> dict[str, Any]:
    contract = _contract(name)
    return yaml.safe_load((ROOT / str(contract["snapshot"])).read_text())


def _validator(spec_name: str, schema_name: str) -> Draft202012Validator:
    spec = _spec(spec_name)
    uri = f"https://benchling.example/{spec_name}.yaml"
    registry = Registry().with_resource(
        uri, Resource.from_contents(spec, default_specification=DRAFT202012)
    )
    return Draft202012Validator(
        {"$ref": f"{uri}#/components/schemas/{schema_name}"},
        registry=registry,
    )


def _schema_errors(spec_name: str, schema_name: str, instance: Any) -> list[str]:
    validator = _validator(spec_name, schema_name)
    return [
        error.message
        for error in sorted(validator.iter_errors(instance), key=lambda item: list(item.path))
    ]


def _request_schema(call: Any) -> tuple[str, str] | None:
    if call.method == "POST" and _V3_ENTRY_CREATE.search(call.path):
        return "benchling-v3", "CreateEntryInput"
    if call.method == "PATCH" and _V3_ENTRY.search(call.path):
        return "benchling-v3", "UpdateEntryInput"
    if call.method == "PATCH" and _V3_RESULT.search(call.path):
        return "benchling-v3", "UpdateResultInput"
    if call.method == "POST" and _V2_BULK.search(call.path):
        return None
    for pattern, schema_name in _V2_BLOB_ROUTES:
        if call.method == "POST" and pattern.search(call.path):
            return "benchling-v2", schema_name
    raise AssertionError(f"no request schema for {call.method} {call.path}")


def _bulk_create_errors(body: dict[str, Any]) -> list[str]:
    """Validate a bulk-create body against the two schemas the V2 contract composes.

    `AssayResultsBulkCreateInTableRequest` is an allOf of a schema that forbids
    additional properties and a schema that adds `tableId`. Standard JSON Schema
    therefore rejects `tableId`, which the live API requires. Check each published
    branch, and record that the composed schema still rejects the live body.
    """
    composed = _schema_errors("benchling-v2", "AssayResultsBulkCreateInTableRequest", body)
    if not composed:
        return []
    if not any("tableId" in message for message in composed):
        return composed
    if not isinstance(body.get("tableId"), str) or not body["tableId"]:
        return ["tableId must be the string declared on AssayResultsBulkCreateInTableRequest"]
    without_table = {key: value for key, value in body.items() if key != "tableId"}
    return _schema_errors("benchling-v2", "AssayResultsBulkCreateRequest", without_table)


def test_retired_document_coordinates_fail_the_current_schema() -> None:
    """The drift that sent `coordinates` must be rejected by the vendored schema."""
    body = {
        "partsMutation": {
            "operationType": "insert",
            "version": "etrver_example",
            "coordinates": {"position": "end_of_document"},
            "parts": [{"contentType": "text", "text": "hello"}],
        }
    }
    errors = _schema_errors("benchling-v3", "UpdateEntryInput", body)
    assert errors
    assert any("coordinates" in message for message in errors)


async def test_every_client_request_body_matches_the_openapi_schema(
    runtime, fake, tmp_path, monkeypatch
) -> None:
    text = {"contentType": "text", "text": "introduced"}
    await _apply(
        runtime,
        "document",
        {"operation": "insert", "position": "end_of_document", "parts": [text]},
    )
    await _apply(
        runtime,
        "document",
        {
            "operation": "move",
            "from_part_id": "docpart_sec2",
            "position": "beginning_of_document",
        },
    )
    await _apply(runtime, "document", {"operation": "delete", "from_part_id": "docpart_text2"})
    await _apply(
        runtime, "document", {"operation": "replace_text", "part_id": TEXT_PART, "text": "edited"}
    )
    await _apply(
        runtime,
        "table",
        {
            "operation": "set_cells",
            "table_part_id": TABLE_PART,
            "updates": [{"row": 0, "column": 1, "value": "1.25"}],
        },
    )
    await _apply(
        runtime,
        "table",
        {"operation": "append_rows", "table_part_id": TABLE_PART, "rows": [["D", "4"]]},
    )
    await _apply(runtime, "fields", {"schema_fields": {"temperature": {"value": 21}}})
    entry_plan = await runtime.planner.plan(
        None,
        "entry",
        {
            "entry_template_id": "etpl_abc123",
            "folder_id": "lib_abc123",
            "name": "Fresh experiment",
            "schema_fields": {"temperature": {"value": 21}},
        },
    )
    await runtime.planner.apply(entry_plan.id)
    await _apply(
        runtime,
        "results",
        {
            "table_id": "strtbl_example",
            "assay_results": [
                {
                    "schemaId": "assaysch_abc123",
                    "fields": {"ct": {"value": 12}, "note": {"value": "n"}},
                }
            ],
        },
    )
    fake.field_defs["assaysch_abc123"].append(
        {
            "id": "assayfield_plot",
            "systemName": "plot",
            "name": "Plot",
            "__typename": "AttachmentLinkFieldDefinition",
            "derivationType": "DIRECT",
        }
    )
    small = tmp_path / "small.csv"
    small.write_bytes(b"a,b\n1,2\n")
    large = tmp_path / "large.bin"
    large.write_bytes(bytes(20))
    monkeypatch.setattr(blobs, "SINGLE_UPLOAD_MAX_BYTES", 10)
    monkeypatch.setattr(blobs, "PART_BYTES", 8)
    await _apply(
        runtime,
        "results",
        {
            "table_id": "strtbl_example",
            "assay_results": [
                {
                    "schemaId": "assaysch_abc123",
                    "fields": {"ct": {"value": 1}, "plot": {"value": {"file": str(small)}}},
                },
                {
                    "schemaId": "assaysch_abc123",
                    "fields": {"ct": {"value": 2}, "plot": {"value": {"file": str(large)}}},
                },
            ],
        },
    )
    fake.results = [
        {
            "id": "result_1",
            "modifiedAt": "2026-01-02T00:00:00Z",
            "entry": {"id": ENTRY_ID},
            "schema": {"id": "assaysch_abc123"},
            "schemaFields": [],
        }
    ]
    csv_path = tmp_path / "update.csv"
    csv_path.write_text("id,Ct\nresult_1,15\n", encoding="utf-8")
    await _apply(
        runtime,
        "results",
        {
            "operation": "update",
            "csv_path": str(csv_path),
            "id_column": "id",
            "column_map": {"Ct": "Ct"},
        },
    )

    writes = [call for call in fake.calls if call.method != "GET" and call.path != "/oauth/token"]
    assert writes
    operations = set()
    for call in writes:
        assert isinstance(call.body, dict)
        schema = _request_schema(call)
        if schema is None:
            errors = _bulk_create_errors(call.body)
        else:
            errors = _schema_errors(schema[0], schema[1], call.body)
        assert errors == [], (call.method, call.path, errors, call.body)
        mutation = call.body.get("partsMutation")
        if isinstance(mutation, dict):
            operations.add(mutation["operationType"])
            assert "coordinates" not in mutation
            assert "sourceCoordinates" not in mutation
            assert "destinationCoordinates" not in mutation
    assert operations == {
        "insert",
        "delete",
        "move",
        "update_table_cells",
        "insert_table_rows",
    }
    field_bodies = [call.body["schemaFields"] for call in writes if "schemaFields" in call.body]
    assert {"tsf_temp": {"value": 21}} in field_bodies
    assert {"assayfield_ct": {"value": 15}} in field_bodies


async def _apply(runtime, family: str, request: dict[str, Any]) -> None:
    planned = await runtime.planner.plan(ENTRY_ID, family, request)
    await runtime.planner.apply(planned.id)


LISTS = json.loads((ROOT / "contracts" / "list-parameters.json").read_text(encoding="utf-8"))


def _allowed_for(method: str, path: str) -> set[str] | None:
    """Parameters recorded for this route, including `{id}` templates."""
    routes: dict[str, list[str]] = LISTS["routes"]
    exact = routes.get(f"{method} {path}")
    if exact is not None:
        return set(exact)
    for pattern, params in routes.items():
        declared, separator, route = pattern.partition(" ")
        if not separator or declared != method or "{" not in route:
            continue
        pieces: list[str] = []
        for piece in re.split(r"(\{[^}]+\})", route):
            if piece.startswith("{") and piece.endswith("}"):
                pieces.append("[^/]+")
            else:
                pieces.append(re.escape(piece))
        if re.fullmatch("".join(pieces), path):
            return set(params)
    return None


async def test_list_query_parameters_stay_inside_the_vendored_allow_list(runtime, fake) -> None:
    """Entry, schema, and results-row lists may send only the recorded parameters."""
    await runtime.service.get_entries(entry_ids=["etr_abc123"])
    await runtime.service.get_template("etpl_abc123")
    await runtime.service.list_templates(name="Synthetic template", limit=10)
    await runtime.service.list_entries(
        project="src_abc123",
        schema_id="entsch_abc123",
        name="Synthetic experiment",
        modified_after="2026-01-01",
        modified_before="2026-12-31",
    )
    page = await runtime.service.list_schemas(limit=2)
    await runtime.service.list_schemas(limit=2, cursor=page["next_token"])
    await runtime.service.list_entries(
        folder="lib_abc123",
        project="src_abc123",
        schema_id="entsch_abc123",
        name="Synthetic experiment",
        modified_after="2026-06-01",
        modified_before="2026-06-30",
        cursor="0",
    )
    fake.extra_entries.append(
        {
            "id": "etr_comma",
            "name": "Assay, replicate 1",
            "displayId": "EXP-5",
            "modifiedAt": "2026-07-01T00:00:00Z",
            "schemaId": "entsch_abc123",
            "projectId": "src_abc123",
            "folderId": "lib_abc123",
        }
    )
    await runtime.service.list_entries(name="Assay, replicate 1")
    for choice in EntrySort:
        await runtime.service.list_entries(sort=choice, limit=1)
    fake.add_results_table()
    await runtime.service.list_results_table_rows("docpart_rt1", limit=1, cursor="0")
    await runtime.service.bulk_get_containers(["con_source1", "con_dest1"])
    checked = []
    for call in fake.calls:
        allowed_params = _allowed_for(call.method, call.path)
        if allowed_params is None:
            continue
        assert set(call.params) <= allowed_params, (call.method, call.path, call.params)
        for name, values in LISTS["enums"].get(f"{call.method} {call.path}", {}).items():
            if name in call.params:
                assert call.params[name] in values, (call.path, name, call.params[name])
        checked.append(call)
    assert checked
    bulk_get = [call for call in fake.calls if call.path.endswith("/entries:bulk-get")]
    assert len(bulk_get) == 1
    assert set(bulk_get[0].params) <= {"entryIds", "displayIds"}
    v3_entry_lists = [call for call in fake.calls if call.path.endswith("/entry/items")]
    assert v3_entry_lists
    assert all("folderId" not in call.params for call in v3_entry_lists)
    assert "folderId" not in LISTS["routes"]["GET /api/v3/entry/items"]
    assert any(call.params.get("folderId") == "lib_abc123" for call in fake.calls)


def _query_parameter(spec_name: str, path: str, name: str) -> dict[str, Any]:
    spec = _spec(spec_name)
    for parameter in spec["paths"][path]["get"].get("parameters", []):
        if "$ref" in parameter:
            parameter = spec["components"]["parameters"][parameter["$ref"].split("/")[-1]]
        if parameter["name"] == name and parameter["in"] == "query":
            return parameter
    raise AssertionError(f"{path} has no query parameter {name}")


def test_recorded_list_enums_are_documented_query_values() -> None:
    """Each recorded enum value is in the vendored OpenAPI enum for that parameter."""
    for route, parameters in LISTS["enums"].items():
        method, _, path = route.partition(" ")
        assert method == "GET"
        version, _, api_path = path.removeprefix("/api/").partition("/")
        for name, values in parameters.items():
            assert name in LISTS["routes"][route]
            schema = _query_parameter(f"benchling-{version}", f"/{api_path}", name)["schema"]
            assert set(values) <= set(schema["enum"]), (route, name)
    sort = _query_parameter("benchling-v2", "/entries", "sort")["schema"]
    assert sort["default"] == "modifiedAt:desc"
    assert sorted(ENTRY_SORTS.values()) == sorted(LISTS["enums"]["GET /api/v2/entries"]["sort"])


INVENTORY_ROUTES = {
    "/containers",
    "/containers/{container_id}",
    "/registries",
    "/registries/{registry_id}/registered-entities:bulk-get",
    "/locations/{location_id}",
    "/boxes/{box_id}",
    "/plates/{plate_id}",
}


def _template_pattern(route: str) -> str:
    pieces = re.split(r"(\{[^}]+\})", route)
    return "".join("[^/]+" if piece.startswith("{") else re.escape(piece) for piece in pieces)


def _query_parameters(spec: dict[str, Any], operation: dict[str, Any]) -> set[str]:
    names = set()
    for parameter in operation.get("parameters", []):
        if "$ref" in parameter:
            parameter = spec["components"]["parameters"][parameter["$ref"].rsplit("/", 1)[-1]]
        if parameter.get("in") == "query":
            names.add(parameter["name"])
    return names


def test_every_v2_route_used_is_a_vendored_path() -> None:
    paths = _spec("benchling-v2")["paths"]
    routes = _contract("benchling-v2")["routes_used"]
    assert INVENTORY_ROUTES <= set(routes)
    for route in routes:
        assert route in paths, route


async def test_inventory_reads_use_listed_v2_routes_and_parameters(runtime, fake) -> None:
    """Container find and location reads send only routes and query names the V2 spec declares."""
    fake.add_stored_containers()
    await runtime.service.find_containers(barcode="BAR-SOURCE", limit=10)
    await runtime.service.find_containers(barcode=["BAR-SOURCE", "BAR-DEST"])
    await runtime.service.find_containers(name="Boxed tube")
    await runtime.service.find_containers(registry_ids=["REG-0001"])
    await runtime.service.find_containers(
        registry_ids=["REG-0001"], registry="src_registry1", in_storage="loc_freezer1"
    )
    await runtime.service.find_containers(contents=["ent_sample1"], in_storage="box_rack1")
    await runtime.service.find_containers(name="Boxed tube", in_storage="box_rack1")
    await runtime.service.get_container("con_well1", location=True)
    await runtime.service.get_container("con_boxed1", location=True)
    spec = _spec("benchling-v2")
    routes = _contract("benchling-v2")["routes_used"]
    used = set()
    for call in fake.calls:
        if not call.path.startswith("/api/v2/"):
            continue
        assert call.method == "GET", call.path
        path = call.path.removeprefix("/api/v2")
        matches = [route for route in routes if re.fullmatch(_template_pattern(route), path)]
        assert len(matches) == 1, (call.path, matches)
        used.add(matches[0])
        declared = _query_parameters(spec, spec["paths"][matches[0]]["get"])
        assert set(call.params) <= declared, (call.path, call.params)
        allowed = _allowed_for("GET", call.path)
        assert allowed is not None, call.path
        assert set(call.params) <= allowed, (call.path, call.params)
    assert used == INVENTORY_ROUTES
