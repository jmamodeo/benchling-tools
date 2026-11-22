"""Entry, schema, and dropdown discovery against the fake API."""

from __future__ import annotations

import base64
import json

import pytest
from fake_benchling import ENTRY_ID, Call

from benchling_tools.errors import UpstreamError, ValidationError
from benchling_tools.listing import (
    _decode_cursor,
    dropdown_id_of,
    entry_summary,
    schema_collection,
    utc_timestamp,
)


def _params(fake, suffix: str) -> list[dict[str, str]]:
    return [call.params for call in fake.calls if call.path.endswith(suffix)]


async def test_v3_filters_are_server_side_and_folder_is_not_sent(runtime, fake) -> None:
    page = await runtime.service.list_entries(
        project=" src_abc123 ",
        schema_id="entsch_abc123",
        name="synthetic experiment",
        modified_after="2026-06-15T08:00:00-04:00",
        modified_before="2026-06-15",
    )
    assert [item["id"] for item in page["items"]] == [ENTRY_ID]
    assert page["items"][0]["folder_id"] == "lib_abc123"
    assert page["items"][0]["project_id"] == "src_abc123"
    assert page["items"][0]["schema_id"] == "entsch_abc123"
    assert page["next_token"] is None
    assert page["routing"] == {
        "api_version": 3,
        "path": "/entry/items",
        "server_side": ["project", "schema", "name", "modified_at"],
        "client_side": [],
    }
    (params,) = _params(fake, "/entry/items")
    assert params == {
        "pageSize": "50",
        "project.eq": "src_abc123",
        "schema.eq": "entsch_abc123",
        "name.anyOf": "synthetic experiment",
        "modifiedAt.gte": "2026-06-15T12:00:00Z",
        "modifiedAt.lte": "2026-06-15T23:59:59Z",
    }
    assert "folderId" not in params


async def test_folder_filter_uses_v2_for_the_whole_query(runtime, fake) -> None:
    page = await runtime.service.list_entries(
        folder="lib_abc123",
        project="src_abc123",
        schema_id="entsch_other",
        name="late note",
        modified_after="2026-11-01",
        modified_before="2026-11-01T08:30:00Z",
    )
    assert [item["id"] for item in page["items"]] == ["etr_list4"]
    assert page["routing"]["api_version"] == 2
    assert page["routing"]["server_side"] == ["folder", "project", "schema", "name", "modified_at"]
    assert page["routing"]["client_side"] == []
    assert _params(fake, "/api/v3/entry/items") == []
    (params,) = _params(fake, "/api/v2/entries")
    assert params == {
        "pageSize": "50",
        "folderId": "lib_abc123",
        "projectId": "src_abc123",
        "schemaId": "entsch_other",
        "names.anyOf": "late note",
        "modifiedAt": ">= 2026-11-01T00:00:00Z AND <= 2026-11-01T08:30:00Z",
    }


async def test_entry_pages_follow_upstream_tokens_until_the_limit(runtime, fake) -> None:
    fake.entry_page_size = 1
    first = await runtime.service.list_entries(limit=2)
    assert [item["id"] for item in first["items"]] == [ENTRY_ID, "etr_list2"]
    assert first["next_token"] == "2"
    assert len(_params(fake, "/entry/items")) == 2
    second = await runtime.service.list_entries(limit=2, cursor=first["next_token"])
    assert [item["id"] for item in second["items"]] == ["etr_list3", "etr_list4"]
    assert second["next_token"] is None
    assert len(_params(fake, "/entry/items")) == 4


async def test_default_entry_page_is_fifty_and_a_timestamp_needs_a_zone(runtime, fake) -> None:
    await runtime.service.list_entries()
    (params,) = _params(fake, "/entry/items")
    assert params == {"pageSize": "50"}
    fake.calls.clear()
    with pytest.raises(ValidationError, match="no time zone"):
        await runtime.service.list_entries(modified_after="2026-06-15T12:00:00")
    with pytest.raises(ValidationError, match="no time zone"):
        await runtime.service.list_entries(modified_before="2026-06-15T12:00:00")
    assert fake.calls == []


@pytest.mark.parametrize(
    "kwargs",
    [
        {"limit": 0},
        {"limit": 101},
        {"limit": True},
        {"limit": 1.5},
        {"name": ""},
        {"name": 1},
        {"project": "src_a,src_b"},
        {"project": "  "},
        {"folder": ""},
        {"cursor": ""},
        {"cursor": 1},
        {"modified_after": ""},
        {"modified_after": 5},
        {"modified_after": "yesterday"},
        {"modified_after": "2026-06-15T12:00:00"},
        {"modified_after": "2026-12-01", "modified_before": "2026-01-01"},
    ],
)
async def test_entry_list_rejects_bad_input_before_calling_the_api(runtime, fake, kwargs) -> None:
    with pytest.raises(ValidationError):
        await runtime.service.list_entries(**kwargs)
    assert fake.calls == []


def _comma_entry() -> dict[str, str]:
    return {
        "id": "etr_comma",
        "name": "Assay, replicate 1",
        "displayId": "EXP-5",
        "modifiedAt": "2026-07-01T00:00:00Z",
        "schemaId": "entsch_abc123",
        "projectId": "src_abc123",
        "folderId": "lib_abc123",
    }


async def test_a_name_containing_a_comma_uses_the_v2_name_parameter(runtime, fake) -> None:
    fake.extra_entries.append(_comma_entry())
    page = await runtime.service.list_entries(
        name="Assay, replicate 1",
        project="src_abc123",
        modified_after="2026-07-01T00:00:00Z",
    )
    assert [item["id"] for item in page["items"]] == ["etr_comma"]
    assert page["items"][0]["modified_at"] == "2026-07-01T00:00:00Z"
    assert page["routing"]["api_version"] == 2
    assert page["routing"]["path"] == "/entries"
    assert page["routing"]["client_side"] == []
    assert _params(fake, "/api/v3/entry/items") == []
    (params,) = _params(fake, "/api/v2/entries")
    assert params["name"] == "Assay, replicate 1"
    assert params["projectId"] == "src_abc123"
    assert params["modifiedAt"] == ">= 2026-07-01T00:00:00Z"
    assert "names.anyOf" not in params
    assert "name.anyOf" not in params


async def test_a_comma_name_with_a_folder_stays_on_one_v2_query(runtime, fake) -> None:
    fake.extra_entries.append(_comma_entry())
    page = await runtime.service.list_entries(folder="lib_abc123", name="Assay, replicate 1")
    assert [item["id"] for item in page["items"]] == ["etr_comma"]
    (params,) = _params(fake, "/api/v2/entries")
    assert params["folderId"] == "lib_abc123"
    assert params["name"] == "Assay, replicate 1"
    assert "names.anyOf" not in params


async def test_identifier_filters_still_reject_commas(runtime, fake) -> None:
    with pytest.raises(ValidationError, match="project must not contain a comma"):
        await runtime.service.list_entries(project="src_a,src_b")
    with pytest.raises(ValidationError, match="folder must not contain a comma"):
        await runtime.service.list_entries(folder="lib_a,lib_b")
    with pytest.raises(ValidationError, match="schema must not contain a comma"):
        await runtime.service.list_entries(schema_id="entsch_a,entsch_b")
    assert fake.calls == []


async def test_listing_timestamps_are_normalized_to_utc_z(runtime, fake) -> None:
    fake.payload_overrides.append(
        (
            "/entry/items",
            200,
            {
                "items": [
                    {"id": "etr_z", "modifiedAt": "2026-06-15T12:00:00+00:00"},
                    {"id": "etr_off", "modifiedAt": "2026-06-15T08:00:00-04:00"},
                    {"id": "etr_frac", "modifiedAt": "2026-06-15T12:00:00.500+00:00"},
                    {"id": "etr_already", "modifiedAt": "2026-06-15T12:00:00Z"},
                    {"id": "etr_naive", "modifiedAt": "2026-06-15T12:00:00"},
                    {"id": "etr_blank", "name": "No stamp"},
                ],
                "nextToken": "",
            },
        )
    )
    page = await runtime.service.list_entries()
    stamps = {item["id"]: item["modified_at"] for item in page["items"]}
    assert stamps == {
        "etr_z": "2026-06-15T12:00:00Z",
        "etr_off": "2026-06-15T12:00:00Z",
        "etr_frac": "2026-06-15T12:00:00.500Z",
        "etr_already": "2026-06-15T12:00:00Z",
        "etr_naive": "2026-06-15T12:00:00",
        "etr_blank": None,
    }
    status, payload = fake._list_entries_v2(Call("GET", "/api/v2/entries", {}, None))
    assert status == 200
    assert payload["entries"][0]["modifiedAt"].endswith("+00:00")
    listed = await runtime.service.list_entries(folder="lib_abc123", limit=1)
    assert listed["items"][0]["modified_at"].endswith("Z")


async def test_schema_timestamps_are_normalized_to_utc_z(runtime, fake) -> None:
    fake.payload_overrides.append(
        (
            "/entry-schema/items",
            200,
            {
                "items": [
                    {
                        "id": "entsch_z",
                        "name": "Zed",
                        "modifiedAt": "2026-01-01T00:00:00+00:00",
                    }
                ],
                "nextToken": "",
            },
        )
    )
    page = await runtime.service.list_schemas(kind="entry", limit=1)
    assert page["items"][0]["modified_at"] == "2026-01-01T00:00:00Z"


def test_utc_timestamp_leaves_non_timestamps_unchanged() -> None:
    assert utc_timestamp(None) is None
    assert utc_timestamp(5) == 5
    assert utc_timestamp("") == ""
    assert utc_timestamp("yesterday") == "yesterday"
    assert utc_timestamp("2026-06-15T12:00:00") == "2026-06-15T12:00:00"


async def test_entry_list_rejects_a_malformed_page_a_repeated_token_and_an_oversized_page(
    runtime, fake
) -> None:
    fake.payload_overrides.append(("/entry/items", 200, {"items": "nope"}))
    with pytest.raises(UpstreamError, match="unexpected shape"):
        await runtime.service.list_entries()

    fake.payload_overrides.append(("/entry/items", 200, {"items": ["x"], "nextToken": ""}))
    with pytest.raises(UpstreamError, match="non-object"):
        await runtime.service.list_entries()

    fake.payload_overrides.append(("/entry/items", 200, {"items": [], "nextToken": 1}))
    with pytest.raises(UpstreamError, match="not a string"):
        await runtime.service.list_entries()

    fake.payload_overrides.append(
        (
            "/entry/items",
            200,
            {"items": [{"id": "etr_a"}, {"id": "etr_b"}], "nextToken": ""},
        )
    )
    with pytest.raises(UpstreamError, match="more items than pageSize"):
        await runtime.service.list_entries(limit=1)

    fake.repeat_next_token = True
    fake.entry_page_size = 1
    with pytest.raises(UpstreamError, match="repeated pagination token"):
        await runtime.service.list_entries(limit=5)


async def test_schema_list_walks_every_kind_and_every_upstream_page(runtime, fake) -> None:
    fake.schema_page_size = 2
    seen: list[str] = []
    cursor = None
    pages = 0
    while True:
        page = await runtime.service.list_schemas(limit=3, cursor=cursor)
        pages += 1
        seen.extend(item["id"] for item in page["items"])
        assert page["kinds"] == ["entry", "entity", "result", "run"]
        cursor = page["next_token"]
        if cursor is None:
            break
    assert pages > 1
    assert seen == [
        "entsch_abc123",
        "entsch_two",
        "entsch_three",
        "entsch_four",
        "entsch_choice",
        "es_target",
        "assaysch_abc123",
        "assaysch_two",
        "assaysch_three",
        "runsch_abc123",
        "runsch_two",
    ]
    entry_calls = _params(fake, "/entry-schema/items")
    assert len(entry_calls) >= 3
    assert all(set(params) <= {"pageSize", "nextToken"} for params in entry_calls)


async def test_schema_list_kind_and_cursor_stay_on_one_collection(runtime, fake) -> None:
    fake.schema_page_size = 1
    first = await runtime.service.list_schemas(kind="run", limit=1)
    assert [item["id"] for item in first["items"]] == ["runsch_abc123"]
    assert first["kinds"] == ["run"]
    second = await runtime.service.list_schemas(kind="run", limit=1, cursor=first["next_token"])
    assert [item["id"] for item in second["items"]] == ["runsch_two"]
    assert second["next_token"] is None
    assert _params(fake, "/entry-schema/items") == []
    assert _params(fake, "/result-schema/items") == []


async def test_schema_list_includes_entity_schemas(runtime, fake) -> None:
    page = await runtime.service.list_schemas(kind="entity")
    assert [item["id"] for item in page["items"]] == ["es_target"]
    assert page["kinds"] == ["entity"]
    assert [call.path for call in fake.calls if call.path.endswith("/items")] == [
        "/api/v3/entity-schema/items"
    ]
    found = await runtime.service.find_schema("Target entity schema", kind="entity")
    assert found["kind"] == "entity"
    assert any(
        call.path == "/api/v3/entity-schema/es_target/field-definitions/items"
        for call in fake.calls
    )


def test_schema_collection_maps_entity_schema_ids() -> None:
    assert schema_collection("es_target") == "entity-schema"


def test_schema_cursor_with_empty_upstream_token_decodes_as_first_page() -> None:
    payload = (
        base64.urlsafe_b64encode(json.dumps({"kind": "entry", "token": ""}).encode())
        .decode()
        .rstrip("=")
    )
    assert _decode_cursor(payload, ("entry",)) == (0, None)


async def test_schema_list_filters_by_exact_name_across_requested_kinds(runtime, fake) -> None:
    page = await runtime.service.list_schemas(name="Second result schema")
    assert page["items"] == [
        {
            "id": "assaysch_two",
            "name": "Second result schema",
            "kind": "result",
            "modified_at": "2026-02-02T00:00:00Z",
        }
    ]
    assert _params(fake, "/entry-schema/items")[0] == {
        "pageSize": "50",
        "name.anyOf.caseSensitive": "Second result schema",
    }


async def test_find_schema_returns_field_types_required_flags_and_dropdown_options(
    runtime, fake
) -> None:
    data = await runtime.service.find_schema("Schema assaysch_abc123", kind="result")
    fields = {field["systemName"]: field for field in data["field_definitions"]}
    assert data["kind"] == "result"
    assert fields["ct"]["type"] == "INTEGER"
    assert fields["ct"]["is_required"] is True
    assert fields["status"]["type"] == "DROPDOWN"
    assert fields["status"]["dropdown_options"][0]["id"] == "sfso_open"
    assert fake.writes() == []


async def test_find_schema_not_found_and_ambiguous_candidates(runtime, fake) -> None:
    from benchling_tools.errors import NotFoundError

    with pytest.raises(NotFoundError, match="No schema named"):
        await runtime.service.find_schema("No such schema")
    with pytest.raises(NotFoundError, match="No schema named"):
        await runtime.service.find_schema("second entry schema")
    fake.schema_catalog["entity"].append({"id": "es_duplicate", "name": "Second entry schema"})
    with pytest.raises(ValidationError, match="is ambiguous") as caught:
        await runtime.service.find_schema("Second entry schema")
    assert caught.value.details["candidates"] == [
        {"id": "entsch_two", "kind": "entry"},
        {"id": "es_duplicate", "kind": "entity"},
    ]
    assert all(set(candidate) == {"id", "kind"} for candidate in caught.value.details["candidates"])


@pytest.mark.parametrize(
    "kwargs",
    [
        {"kind": "sample"},
        {"kind": ""},
        {"name": ""},
        {"name": "Result, schema"},
        {"limit": 0},
        {"cursor": "%%%"},
        {"cursor": ""},
    ],
)
async def test_schema_list_rejects_bad_input_before_calling_the_api(runtime, fake, kwargs) -> None:
    with pytest.raises(ValidationError):
        await runtime.service.list_schemas(**kwargs)
    assert fake.calls == []


async def test_schema_cursor_from_another_kind_is_rejected(runtime, fake) -> None:
    page = await runtime.service.list_schemas(limit=1)
    with pytest.raises(ValidationError, match="does not match"):
        await runtime.service.list_schemas(kind="result", cursor=page["next_token"])


async def test_schema_list_rejects_a_repeated_token_and_an_oversized_page(runtime, fake) -> None:
    fake.repeat_next_token = True
    fake.schema_page_size = 1
    with pytest.raises(UpstreamError, match="repeated pagination token"):
        await runtime.service.list_schemas(kind="entry", limit=4)

    fake.repeat_next_token = False
    fake.payload_overrides.append(
        (
            "/entry-schema/items",
            200,
            {"items": [{"id": "entsch_a"}, {"id": "entsch_b"}], "nextToken": ""},
        )
    )
    with pytest.raises(UpstreamError, match="more items than pageSize"):
        await runtime.service.list_schemas(kind="entry", limit=1)


async def test_get_schema_attaches_dropdown_options_once_per_dropdown(runtime, fake) -> None:
    fake.field_defs["entsch_choice"].append(
        {
            "id": "tsf_direct",
            "systemName": "direct",
            "name": "Direct",
            "__typename": "DropdownLinkFieldDefinition",
            "derivationType": "DIRECT",
            "dropdownId": "sfs_status",
        }
    )
    data = await runtime.service.get_schema("entsch_choice")
    status, again, orphan, direct = data["field_definitions"]
    options = [
        {"id": "sfso_open", "name": "Open", "archived": False},
        {"id": "sfso_closed", "name": "Closed", "archived": True},
    ]
    assert status["dropdown"] == {"id": "sfs_status", "name": "Status", "options": options}
    assert again["dropdown"]["id"] == "sfs_status"
    assert direct["dropdown"]["options"] == options
    assert "dropdown" not in orphan
    dropdown_gets = [call for call in fake.calls if call.path.endswith("/dropdown/sfs_status")]
    option_gets = [
        call for call in fake.calls if call.path.endswith("/dropdown/sfs_status/options/items")
    ]
    assert len(dropdown_gets) == 1
    assert len(option_gets) == 1


async def test_get_schema_reads_a_run_schema(runtime, fake) -> None:
    data = await runtime.service.get_schema("runsch_abc123")
    assert data["schema"] == {"id": "runsch_abc123", "name": "Schema runsch_abc123"}
    assert data["field_definitions"][0]["systemName"] == "note"
    assert any(call.path == "/api/v3/run-schema/runsch_abc123" for call in fake.calls)


async def test_dropdown_options_must_be_objects(runtime, fake) -> None:
    fake.payload_overrides.append(("/dropdown/sfs_status/options/items", 200, {"items": "nope"}))
    with pytest.raises(UpstreamError, match="unexpected shape"):
        await runtime.service.get_schema("entsch_choice")
    fake.payload_overrides.append(("/dropdown/sfs_status/options/items", 200, {"items": ["Open"]}))
    with pytest.raises(UpstreamError, match="non-object dropdown option"):
        await runtime.service.get_schema("entsch_choice")


def test_entry_summary_accepts_flat_and_nested_ids() -> None:
    nested = entry_summary(
        {
            "id": "etr_nested",
            "schema": "entsch_text",
            "folder": {"id": "lib_nested"},
            "project": {"id": "src_nested"},
        }
    )
    assert nested["schema_id"] == "entsch_text"
    assert nested["folder_id"] == "lib_nested"
    assert nested["project_id"] == "src_nested"
    flat = entry_summary(
        {"schemaId": "entsch_flat", "folderId": "lib_flat", "projectId": "src_flat"}
    )
    assert flat["schema_id"] == "entsch_flat"
    assert flat["folder_id"] == "lib_flat"
    assert flat["project_id"] == "src_flat"


def test_dropdown_id_prefers_a_direct_id_and_ignores_blanks() -> None:
    assert dropdown_id_of({"dropdownId": "sfs_status", "linkDefinition": {"id": "sfs_other"}}) == (
        "sfs_status"
    )
    assert dropdown_id_of({"linkDefinition": {"id": "sfs_status"}}) == "sfs_status"
    assert dropdown_id_of({"dropdownId": "", "linkDefinition": {"id": ""}}) is None
    assert dropdown_id_of({"linkDefinition": "sfs_status"}) is None
