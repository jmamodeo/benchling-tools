from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
from conftest import runtime_for
from fake_benchling import ENTRY_ID, TABLE_PART, TEXT_PART, entry_web_url
from mcp.shared.memory import create_connected_server_and_client_session as Client

from benchling_tools.errors import AuthenticationError, ValidationError
from benchling_tools.mcp_server import build_mcp_server, run_mcp_server
from benchling_tools.runtime import RuntimePool

READ_TOOLS = {
    "auth_status",
    "doctor",
    "entry_resolve",
    "entry_get",
    "entry_inspect",
    "entry_bulk_get",
    "folder_get",
    "template_get",
    "template_list",
    "section_get",
    "part_get",
    "table_get",
    "entry_list",
    "schema_get",
    "schema_list",
    "schema_find",
    "entry_fields_get",
    "results_list",
    "results_get",
    "blob_get",
    "results_tables",
    "results_rows",
    "results_pending",
    "results_verify",
    "container_get",
    "container_find",
    "container_schema_list",
    "changes_status",
}


async def call(client, tool_name, **arguments):
    result = await client.call_tool(tool_name, arguments)
    assert result.isError is False, result
    payload = result.structuredContent
    assert set(payload) == {"schema_version", "ok", "data", "error", "meta"}
    return payload


class Session:
    """Opens a short-lived in-process MCP connection per request.

    Entering the client inside an async fixture breaks anyio cancel scopes, and the
    server keeps no per-connection state, so connecting per call is equivalent.
    """

    def __init__(self, server) -> None:
        self.server = server

    async def call_tool(self, name, arguments):
        async with Client(self.server._mcp_server) as active:
            return await active.call_tool(name, arguments)

    async def list_tools(self):
        async with Client(self.server._mcp_server) as active:
            return await active.list_tools()


@pytest.fixture
def client(pool):
    return Session(build_mcp_server(pool))


@pytest.fixture
def writer(pool):
    return Session(build_mcp_server(pool, writable_envs={"sandbox"}))


async def test_tools_are_annotated_by_whether_they_can_write(client) -> None:
    tools = {tool.name: tool for tool in (await client.list_tools()).tools}
    assert READ_TOOLS | {
        "blob_download",
        "export",
        "changes_plan",
        "changes_apply",
        "changes_export",
        "issue_draft",
    } == set(tools)
    for name in READ_TOOLS:
        assert tools[name].annotations.readOnlyHint is True, name
    assert tools["changes_apply"].annotations.destructiveHint is True
    assert tools["changes_export"].annotations.readOnlyHint is False
    assert tools["changes_export"].annotations.destructiveHint is False
    assert tools["changes_plan"].annotations.readOnlyHint is False
    assert tools["changes_plan"].annotations.destructiveHint is False
    assert tools["export"].annotations.readOnlyHint is False
    assert tools["export"].annotations.destructiveHint is False
    assert tools["blob_download"].annotations.readOnlyHint is False
    assert tools["blob_download"].annotations.destructiveHint is False
    schema = tools["doctor"].outputSchema
    assert schema["properties"]["schema_version"]["const"] == "1.0"
    assert "ok" in schema["required"]
    assert tools["issue_draft"].annotations.readOnlyHint is True
    for name in set(tools) - {"auth_status", "issue_draft"}:
        assert "env" in tools[name].inputSchema["required"], name


async def test_skill_md_lists_every_mcp_tool(client) -> None:
    skill = (Path(__file__).resolve().parents[1] / "SKILL.md").read_text(encoding="utf-8")
    line = next(row for row in skill.splitlines() if row.startswith("Tools for this workflow:"))
    documented = set(re.findall(r"`(\w+)`", line.split(".", 1)[0]))
    tools = {tool.name for tool in (await client.list_tools()).tools}
    assert documented == tools


async def test_get_blob_reads_metadata_without_writes(client, fake) -> None:
    blob_id = "00000000-0000-4000-8000-000000000009"
    fake.blobs[blob_id] = {
        "id": blob_id,
        "name": "plot.png",
        "type": "VISUALIZATION",
        "mimeType": "image/png",
        "uploadStatus": "COMPLETE",
    }
    payload = await call(client, "blob_get", env="sandbox", blob_id=blob_id)
    assert payload["ok"] is True
    assert payload["data"]["uploadStatus"] == "COMPLETE"
    assert fake.writes() == []


async def test_blob_download_tool(client, fake, tmp_path) -> None:
    blob_id = "00000000-0000-4000-8000-000000000009"
    fake.blobs[blob_id] = {
        "id": blob_id,
        "name": "sample.txt",
        "type": "RAW_FILE",
        "mimeType": "text/plain",
        "uploadStatus": "COMPLETE",
        "data": b"synthetic",
    }
    output = tmp_path / "output"
    payload = await call(
        client,
        "blob_download",
        env="sandbox",
        blob_id=blob_id,
        output=str(output),
        expect_size=9,
    )
    assert payload["ok"] is True
    assert payload["data"]["verified"] is True
    assert output.read_bytes() == b"synthetic"
    assert fake.writes() == []


async def test_missing_env_argument_is_a_protocol_error(client) -> None:
    result = await client.call_tool("entry_get", {"reference": ENTRY_ID})
    assert result.isError is True


async def test_entry_creation_preview_stays_read_only_until_enabled_apply(
    client, writer, fake
) -> None:
    request = {
        "entry_template_id": "etpl_abc123",
        "folder_id": "lib_abc123",
        "name": "Fresh experiment",
    }
    planned = await call(
        client,
        "changes_plan",
        env="sandbox",
        family="entry",
        spec=request,
    )
    assert planned["data"]["after"]["folder"]["name"] == "Synthetic folder"
    assert fake.writes() == []

    blocked = await call(
        client,
        "changes_apply",
        env="sandbox",
        plan_id=planned["data"]["id"],
    )
    assert blocked["ok"] is False
    assert fake.writes() == []

    applied = await call(
        writer,
        "changes_apply",
        env="sandbox",
        plan_id=planned["data"]["id"],
    )
    assert applied["data"]["journal"]["result"]["entry_id"] == "etr_created1"
    assert applied["data"]["web_url"] == entry_web_url("etr_created1")
    assert len(fake.writes()) == 1


async def test_unknown_env_returns_structured_error_with_choices(client, fake) -> None:
    payload = await call(client, "entry_get", env="prod", reference=ENTRY_ID)
    assert payload["ok"] is False
    assert payload["error"]["code"] == "validation_error"
    assert payload["error"]["details"]["available_envs"] == ["sandbox"]
    assert payload["meta"] == {}
    assert fake.calls == []


async def test_list_environments_reports_write_permission(pool, benchling_env, monkeypatch) -> None:
    from conftest import configure_env

    configure_env("other", "https://other-tenant.benchling.com", secret="other-secret-value")
    active = Session(build_mcp_server(pool, writable_envs={"other"}))
    data = (await call(active, "auth_status"))["data"]["environments"]
    assert {name: item["writes_allowed"] for name, item in data.items()} == {
        "other": True,
        "sandbox": False,
    }
    assert "other-secret-value" not in json.dumps(data)
    assert "sandbox-client-secret" not in json.dumps(data)


async def test_capabilities_reports_each_api_check(client, fake) -> None:
    payload = await call(client, "doctor", env="sandbox")
    assert payload["ok"] is True
    assert payload["meta"]["env"] == "sandbox"
    assert payload["data"]["capabilities"]["entry_fields"] is True
    fake.fail("GET", "/entry/items", 403)
    failed = await call(client, "doctor", env="sandbox")
    assert failed["ok"] is False
    assert failed["error"]["code"] == "authentication_error"
    assert failed["meta"]["env"] == "sandbox"
    degraded = failed["error"]["details"]
    assert degraded["ok"] is False
    assert degraded["capabilities"]["document_parts"] is False
    assert degraded["checks"][0]["code"] == "access_denied"


async def test_resolve_entry_by_id_and_display_id(client) -> None:
    by_id = (await call(client, "entry_resolve", env="sandbox", reference=ENTRY_ID))["data"]
    by_display = (await call(client, "entry_resolve", env="sandbox", reference="EXP-1"))["data"]
    assert by_id["resolved_by"] == "canonical_id"
    assert by_display["resolved_by"] == "display_id"
    assert by_id["entry_id"] == by_display["entry_id"] == ENTRY_ID
    missing = await call(client, "entry_resolve", env="sandbox", reference="EXP-99")
    assert missing["error"]["code"] == "not_found"


async def test_container_read_tools_and_plans_keep_default_read_only(client, writer, fake) -> None:
    container = await call(client, "container_get", env="sandbox", container_id="con_source1")
    assert container["data"]["name"] == "Source vial"
    found = await call(client, "container_find", env="sandbox", barcode="BAR-SOURCE", limit=10)
    assert found["data"]["items"][0]["id"] == "con_source1"
    schemas = await call(client, "container_schema_list", env="sandbox")
    assert schemas["data"]["items"][0]["fieldDefinitions"][0]["name"] == "Storage note"

    planned = await call(
        client,
        "changes_plan",
        env="sandbox",
        family="containers",
        spec={
            "operation": "create",
            "schema_id": "contsch_abc123",
            "name": "Synthetic vial from MCP",
        },
    )
    assert planned["ok"] is True, planned
    denied = await call(client, "changes_apply", env="sandbox", plan_id=planned["data"]["id"])
    assert denied["error"]["code"] == "unsupported_capability"
    assert fake.writes() == []
    applied = await call(writer, "changes_apply", env="sandbox", plan_id=planned["data"]["id"])
    assert applied["ok"] is True
    assert [call.path for call in fake.writes()] == ["/api/v2/containers"]


async def test_get_entry_without_full_returns_metadata_only(client, fake) -> None:
    data = (await call(client, "entry_get", env="sandbox", reference=ENTRY_ID))["data"]
    assert data["version_id"] == "ver_1"
    assert "parts" not in data
    assert not any("parts/items" in c.path for c in fake.calls)


async def test_full_entry_pages_parts_results_and_table_rows(client, fake) -> None:
    fake.parts_page_size = 2
    fake.results_page_size = 2
    first = (
        await call(
            client,
            "entry_get",
            env="sandbox",
            reference=ENTRY_ID,
            full=True,
            part_limit=4,
            result_limit=2,
            table_row_limit=1,
        )
    )["data"]
    assert [p["id"] for p in first["parts"]][-1] == TABLE_PART
    assert first["pagination"]["parts"] == {
        "offset": 0,
        "returned": 4,
        "total": 6,
        "next_offset": 4,
    }
    assert first["pagination"]["results"] == {
        "offset": 0,
        "returned": 2,
        "total": 3,
        "next_offset": 2,
    }
    assert first["pagination"]["table_rows"] == {"offset": 0, "limit_per_table": 1}
    assert len(first["tables"][TABLE_PART]["content"]["rows"]) == 1
    assert first["tables"][TABLE_PART]["pagination"] == {
        "offset": 0,
        "returned": 1,
        "total": 3,
        "next_offset": 1,
    }
    second = (
        await call(
            client,
            "entry_get",
            env="sandbox",
            reference=ENTRY_ID,
            full=True,
            part_offset=4,
            part_limit=4,
            result_offset=2,
            table_row_offset=1,
        )
    )["data"]
    assert [p["id"] for p in second["parts"]] == ["docpart_struct1", "docpart_text2"]
    assert second["pagination"]["parts"]["next_offset"] is None
    assert [r["id"] for r in second["results"]] == ["result_3"]
    assert second["tables"] == {}
    assert second["completeness"] == "partial"
    assert {i["reason"] for i in second["issues"]} == {
        "structured_table_detail_not_available_through_document_api"
    }


@pytest.mark.parametrize(
    ("tool", "arguments"),
    [
        ("entry_get", {"reference": ENTRY_ID, "full": True, "part_offset": -1}),
        ("entry_get", {"reference": ENTRY_ID, "full": True, "result_limit": 0}),
        ("entry_get", {"reference": ENTRY_ID, "full": True, "table_row_limit": 0}),
        ("section_get", {"reference": ENTRY_ID, "section": "Results", "part_limit": 0}),
        ("section_get", {"reference": ENTRY_ID, "section": "Results", "table_row_offset": -1}),
        ("part_get", {"reference": ENTRY_ID, "part_id": TEXT_PART, "row_offset": -1}),
        ("part_get", {"reference": ENTRY_ID, "part_id": TEXT_PART, "row_limit": 0}),
        ("table_get", {"part_id": TABLE_PART, "row_offset": -1}),
        ("table_get", {"part_id": TABLE_PART, "row_limit": 0}),
        ("schema_get", {"schema_id": "entsch_abc123", "field_offset": -1}),
        ("schema_get", {"schema_id": "entsch_abc123", "field_limit": 0}),
        ("results_tables", {"reference": ENTRY_ID, "offset": -1}),
        ("results_tables", {"reference": ENTRY_ID, "limit": 101}),
        ("results_tables", {"reference": ENTRY_ID, "limit": 0}),
        ("results_list", {"page_size": 0}),
        ("results_list", {"page_size": 101}),
        ("entry_list", {"limit": 0}),
        ("entry_list", {"limit": 101}),
        ("results_rows", {"part_id": "docpart_rt1", "limit": 0}),
        ("results_rows", {"part_id": "docpart_rt1", "limit": 101}),
        ("results_pending", {"result_ids": ["id1"], "limit": 0}),
        ("results_pending", {}),
        ("results_verify", {"plan_id": "0" * 32, "sample": 0}),
        ("schema_list", {"limit": 0}),
        ("schema_list", {"kind": "sample"}),
    ],
)
async def test_invalid_pagination_arguments_return_validation_envelopes(
    client, fake, tool, arguments
) -> None:
    payload = await call(client, tool, env="sandbox", **arguments)
    assert payload["ok"] is False
    assert payload["error"]["code"] == "validation_error"
    assert payload["meta"]["env"] == "sandbox"
    assert fake.writes() == []


async def test_inspect_entry_summarizes_structure(client) -> None:
    data = (await call(client, "entry_inspect", env="sandbox", reference=ENTRY_ID))["data"]
    assert [s["title"] for s in data["sections"]] == ["Methods", "Results"]
    assert data["tables"][TABLE_PART]["row_count"] == 3
    assert data["tables"][TABLE_PART]["name"] == "Measurements"
    assert data["project_id"] == "src_abc123"
    assert data["result_tables"][0]["table_id"] == "strtbl_example"
    assert data["parts"][1] == {
        "id": TEXT_PART,
        "type": "TextDocumentContent",
        "summary": data["parts"][1]["summary"],
    }


async def test_read_only_lookup_tools_get_entries_folder_and_template(client, fake) -> None:
    fake.entry_templates["etpl_two"] = {
        "id": "etpl_two",
        "name": "Second template",
        "modifiedAt": "2026-02-01T00:00:00Z",
    }
    entries = await call(
        client,
        "entry_bulk_get",
        env="sandbox",
        entry_ids=[ENTRY_ID, "etr_list2"],
    )
    assert [item["id"] for item in entries["data"]["items"]] == [ENTRY_ID, "etr_list2"]
    folder = await call(client, "folder_get", env="sandbox", folder_id="lib_abc123")
    template = await call(client, "template_get", env="sandbox", template_id="etpl_abc123")
    template_page = await call(
        client,
        "template_list",
        env="sandbox",
        name="Synthetic template",
        limit=10,
    )
    first_page = await call(client, "template_list", env="sandbox", limit=1)
    second_page = await call(
        client,
        "template_list",
        env="sandbox",
        limit=1,
        cursor=first_page["data"]["next_token"],
    )
    assert folder["data"]["name"] == "Synthetic folder"
    assert template["data"]["structure"]["results_table_schema_ids"] == ["assaysch_abc123"]
    assert template_page["data"]["items"][0]["id"] == "etpl_abc123"
    assert first_page["data"]["next_token"] == "1"
    assert second_page["data"]["items"][0]["id"] == "etpl_two"
    assert fake.writes() == []


async def test_get_section_by_title_or_part_id_with_paging(client, fake) -> None:
    by_title = (
        await call(
            client,
            "section_get",
            env="sandbox",
            reference=ENTRY_ID,
            section="Results",
            part_limit=2,
        )
    )["data"]
    assert [p["id"] for p in by_title["parts"]] == ["docpart_sec2", TABLE_PART]
    assert by_title["pagination"]["parts"] == {
        "offset": 0,
        "returned": 2,
        "total": 4,
        "next_offset": 2,
    }
    assert TABLE_PART in by_title["tables"]
    by_id = (
        await call(
            client,
            "section_get",
            env="sandbox",
            reference=ENTRY_ID,
            section="docpart_sec2",
            part_offset=2,
        )
    )["data"]
    assert [p["id"] for p in by_id["parts"]] == ["docpart_struct1", "docpart_text2"]
    assert by_id["tables"] == {}
    missing = await call(client, "section_get", env="sandbox", reference=ENTRY_ID, section="Nope")
    assert missing["error"]["code"] == "not_found"


async def test_duplicate_section_titles_require_a_part_id(client, fake) -> None:
    fake.parts[0]["title"] = "Results"
    payload = await call(
        client, "section_get", env="sandbox", reference=ENTRY_ID, section="Results"
    )
    assert payload["error"]["code"] == "validation_error"
    assert payload["error"]["details"]["part_ids"] == ["docpart_sec1", "docpart_sec2"]


async def test_get_part_pages_table_detail_but_not_text(client) -> None:
    table = (
        await call(
            client,
            "part_get",
            env="sandbox",
            reference=ENTRY_ID,
            part_id=TABLE_PART,
            row_offset=1,
            row_limit=1,
        )
    )["data"]
    assert table["detail"]["pagination"] == {
        "offset": 1,
        "returned": 1,
        "total": 3,
        "next_offset": 2,
    }
    assert table["detail"]["content"]["rows"][0]["cells"][0]["inputValue"]["value"] == "B"
    text = (await call(client, "part_get", env="sandbox", reference=ENTRY_ID, part_id=TEXT_PART))[
        "data"
    ]
    assert text["detail"] is None
    assert text["part"]["content"]["text"] == "before"
    missing = await call(client, "part_get", env="sandbox", reference=ENTRY_ID, part_id="docpart_x")
    assert missing["error"]["code"] == "not_found"


async def test_get_table_pages_rows_across_upstream_pages(client, fake) -> None:
    fake.table_page_size = 2
    page = (
        await call(
            client, "table_get", env="sandbox", part_id=TABLE_PART, row_offset=2, row_limit=5
        )
    )["data"]
    assert page["pagination"] == {"offset": 2, "returned": 1, "total": 3, "next_offset": None}
    assert page["versionMetadata"]["versionId"] == "ver_1"
    missing = await call(client, "table_get", env="sandbox", part_id="docpart_unknown")
    assert missing["error"]["code"] == "not_found"


async def test_get_schema_pages_field_definitions(client) -> None:
    first = (
        await call(client, "schema_get", env="sandbox", schema_id="entsch_abc123", field_limit=5)
    )["data"]
    assert [f["systemName"] for f in first["field_definitions"]] == [
        "temperature",
        "notes",
        "ratio",
        "flag",
        "run_date",
    ]
    assert first["pagination"] == {"offset": 0, "returned": 5, "total": 12, "next_offset": 5}
    last = (
        await call(
            client,
            "schema_get",
            env="sandbox",
            schema_id="entsch_abc123",
            field_offset=10,
            field_limit=5,
        )
    )["data"]
    assert last["pagination"]["next_offset"] is None and last["pagination"]["returned"] == 2
    result_schema = (await call(client, "schema_get", env="sandbox", schema_id="assaysch_abc123"))[
        "data"
    ]
    assert result_schema["schema"]["id"] == "assaysch_abc123"
    missing = await call(client, "schema_get", env="sandbox", schema_id="entsch_none")
    assert missing["error"]["code"] == "not_found"


async def test_get_entry_fields_maps_values_by_system_name(client) -> None:
    data = (await call(client, "entry_fields_get", env="sandbox", reference=ENTRY_ID))["data"]
    assert data["fields_by_system_name"]["temperature"] == 20
    assert data["fields_by_system_name"]["flag"] is None
    assert data["project_id"] == "src_abc123"
    assert data["schema"] == {"id": "entsch_abc123"}


async def test_list_results_uses_explicit_cursor_pagination(client, fake) -> None:
    fake.results_page_size = 2
    first = (await call(client, "results_list", env="sandbox", entry="EXP-1", page_size=2))["data"]
    assert [r["id"] for r in first["items"]] == ["result_1", "result_2"]
    assert first["next_token"] == "2"
    second = (
        await call(
            client, "results_list", env="sandbox", entry=ENTRY_ID, cursor=first["next_token"]
        )
    )["data"]
    assert [r["id"] for r in second["items"]] == ["result_3"]
    assert second["next_token"] is None
    sent = [c.params for c in fake.calls if c.path.endswith("/result/items")]
    assert sent[0] == {"pageSize": "2", "entry.eq": ENTRY_ID}
    assert sent[1]["nextToken"] == "2"
    filtered = (await call(client, "results_list", env="sandbox", schema_id="assaysch_none"))[
        "data"
    ]
    assert filtered["items"] == []


async def test_get_result_returns_record_or_not_found(client) -> None:
    assert (await call(client, "results_get", env="sandbox", result_id="result_2"))["data"][
        "id"
    ] == "result_2"
    missing = await call(client, "results_get", env="sandbox", result_id="result_zzz")
    assert missing["error"]["code"] == "not_found"


async def test_results_tools_warn_when_a_table_cell_is_stale(client, fake) -> None:
    fake.add_results_table()
    fake.results_parts["docpart_rt1"]["columnDefinitions"][0]["fieldDefinitionApiId"] = "af_ct"
    fake.results[0]["schemaFields"] = [
        {"fieldDefinition": {"id": "af_ct"}, "value": {"__typename": "IntegerValue", "value": 15}}
    ]
    fake.parts.append(
        {
            "id": "docpart_rt1",
            "__typename": "DocumentPartResultsTable",
            "resultSchema": {"id": "assaysch_abc123"},
        }
    )
    rows = (await call(client, "results_rows", env="sandbox", part_id="docpart_rt1"))["data"]
    assert "Re-sync table" in rows["warnings"][0]
    assert rows["items"][0]["stale_cells"][0]["table_value"] == 12
    got = (await call(client, "results_get", env="sandbox", result_id="result_1"))["data"]
    assert got["table_check"]["status"] == "stale"
    assert "Re-sync table" in got["warnings"][0]
    assert fake.writes() == []


async def test_list_result_tables_pages_targets(client, fake) -> None:
    data = (await call(client, "results_tables", env="sandbox", reference=ENTRY_ID))["data"]
    assert data["items"][0]["table_id"] == "strtbl_example"
    assert data["items"][0]["schema_id"] == "assaysch_abc123"
    assert data["pagination"] == {"offset": 0, "returned": 1, "total": 1, "next_offset": None}
    assert data["id_namespace"] == "v2_structured_table"
    past_end = (await call(client, "results_tables", env="sandbox", reference=ENTRY_ID, offset=1))[
        "data"
    ]
    assert past_end["items"] == [] and past_end["pagination"]["returned"] == 0


async def test_upstream_errors_map_to_envelope_codes(client, fake) -> None:
    for status, code in (
        (401, "authentication_error"),
        (403, "access_denied"),
        (404, "not_found"),
        (400, "validation_error"),
    ):
        fake.fail("GET", f"/entry/{ENTRY_ID}", status)
        payload = await call(client, "entry_resolve", env="sandbox", reference=ENTRY_ID)
        assert payload["error"]["code"] == code, status
        assert payload["error"]["details"]["status"] == status


async def test_unexpected_local_failures_do_not_leak_internals(client, fake) -> None:
    fake.fail("GET", f"/entry/{ENTRY_ID}", 0)
    payload = await call(client, "entry_resolve", env="sandbox", reference=ENTRY_ID)
    assert payload["error"]["code"] == "upstream_error"
    assert payload["error"]["details"] == {"type": "RuntimeError"}
    assert "simulated" not in json.dumps(payload)


async def test_plan_and_status_work_but_apply_is_blocked_by_default(client, fake) -> None:
    planned = await call(
        client,
        "changes_plan",
        env="sandbox",
        entry=ENTRY_ID,
        family="document",
        spec={"operation": "delete", "from_part_id": TEXT_PART},
    )
    assert planned["ok"] is True
    plan_id = planned["data"]["id"]
    blocked = await call(client, "changes_apply", env="sandbox", plan_id=plan_id)
    assert blocked["error"]["code"] == "unsupported_capability"
    assert "--allow-writes sandbox" in blocked["error"]["message"]
    assert fake.writes() == []
    status = (await call(client, "changes_status", env="sandbox", plan_id=plan_id))["data"]
    assert status["journal"]["status"] == "pending"
    assert status["plan"]["family"] == "document"


async def test_changes_export_tool_writes_a_portable_bundle(client, tmp_path) -> None:
    planned = await call(
        client,
        "changes_plan",
        env="sandbox",
        entry=ENTRY_ID,
        family="document",
        spec={"operation": "delete", "from_part_id": TEXT_PART},
    )
    output = tmp_path / "portable-plan.json"

    exported = await call(
        client,
        "changes_export",
        env="sandbox",
        plan_id=planned["data"]["id"],
        output=str(output),
    )

    assert exported["data"]["plan_id"] == planned["data"]["id"]
    bundle = json.loads(output.read_text())
    assert bundle["plan"]["id"] == planned["data"]["id"]
    assert bundle["checksum"] == bundle["plan"]["checksum"]
    assert bundle["journal"]["plan_id"] == planned["data"]["id"]


async def test_plan_validation_errors_are_enveloped(client, fake) -> None:
    payload = await call(
        client,
        "changes_plan",
        env="sandbox",
        entry=ENTRY_ID,
        family="document",
        spec={"operation": "explode"},
    )
    assert payload["error"]["code"] == "validation_error"
    bad_family = await client.call_tool(
        "changes_plan",
        {"env": "sandbox", "entry": ENTRY_ID, "family": "magic", "spec": {}},
    )
    assert bad_family.isError is True


async def test_apply_changes_when_writes_are_enabled(writer, fake) -> None:
    planned = await call(
        writer,
        "changes_plan",
        env="sandbox",
        entry=ENTRY_ID,
        family="table",
        spec={
            "operation": "append_rows",
            "table_part_id": TABLE_PART,
            "rows": [["D", "4"]],
        },
    )
    plan_id = planned["data"]["id"]
    applied = await call(writer, "changes_apply", env="sandbox", plan_id=plan_id)
    assert applied["ok"] is True
    assert applied["data"]["journal"]["status"] == "succeeded"
    assert len(fake.table_content["rows"]) == 4
    again = await call(writer, "changes_apply", env="sandbox", plan_id=plan_id)
    assert again["error"]["code"] == "conflict"
    assert len(fake.writes()) == 1


async def test_apply_conflict_and_partial_completion_are_structured(writer, fake) -> None:
    stale = await call(
        writer,
        "changes_plan",
        env="sandbox",
        entry=ENTRY_ID,
        family="document",
        spec={"operation": "delete", "from_part_id": TEXT_PART},
    )
    fake.entry_version += 1
    conflict = await call(writer, "changes_apply", env="sandbox", plan_id=stale["data"]["id"])
    assert conflict["error"]["code"] == "conflict"
    assert conflict["error"]["details"] == {"expected": "ver_1", "actual": "ver_2"}
    unknown = await call(
        writer,
        "changes_plan",
        env="sandbox",
        entry=ENTRY_ID,
        family="document",
        spec={"operation": "delete", "from_part_id": TEXT_PART},
    )
    fake.fail("PATCH", f"/entry/{ENTRY_ID}", 500)
    outcome = await call(writer, "changes_apply", env="sandbox", plan_id=unknown["data"]["id"])
    assert outcome["error"]["code"] == "partial_or_unknown_completion"
    assert outcome["error"]["details"]["journal"]["status"] == "unknown"


async def test_apply_is_guarded_per_environment(pool, fake, monkeypatch) -> None:
    from conftest import configure_env

    configure_env("other", "https://other-tenant.benchling.com", secret="other-secret-value")
    other = runtime_for(fake, "other")
    both = RuntimePool(runtimes={"sandbox": pool.get("sandbox"), "other": other})
    try:
        active = Session(build_mcp_server(both, writable_envs={"sandbox"}))
        planned = await call(
            active,
            "changes_plan",
            env="other",
            entry=ENTRY_ID,
            family="document",
            spec={"operation": "delete", "from_part_id": TEXT_PART},
        )
        blocked = await call(active, "changes_apply", env="other", plan_id=planned["data"]["id"])
        assert blocked["error"]["code"] == "unsupported_capability"
        wrong_env = await call(
            active, "changes_apply", env="sandbox", plan_id=planned["data"]["id"]
        )
        assert wrong_env["error"]["code"] == "validation_error"
        assert fake.writes() == []
    finally:
        await other.close()


def test_serve_rejects_unknown_writable_env(benchling_env) -> None:
    with pytest.raises(ValidationError) as caught:
        run_mcp_server(writable_envs={"sandbox", "prod"})
    assert caught.value.details == {
        "unknown_envs": ["prod"],
        "available_envs": ["sandbox"],
    }


def test_serve_requires_at_least_one_environment() -> None:
    with pytest.raises(AuthenticationError, match="No environments configured"):
        run_mcp_server()


async def test_list_entries_is_read_only_and_routes_folder_filters_to_v2(client, fake) -> None:
    result = await client.call_tool(
        "entry_list",
        {"env": "sandbox", "folder": "lib_abc123", "name": "Late note", "limit": 10},
    )
    assert result.isError is False, result
    payload = result.structuredContent
    assert set(payload) == {"schema_version", "ok", "data", "error", "meta"}
    data = payload["data"]
    assert [item["id"] for item in data["items"]] == ["etr_list4"]
    assert data["routing"]["api_version"] == 2
    assert data["routing"]["client_side"] == []
    assert fake.writes() == []
    assert all(not call.path.endswith("/entry/items") for call in fake.calls)


async def test_list_results_table_rows_is_read_only_and_paged(client, fake) -> None:
    fake.add_results_table()
    first = (await call(client, "results_rows", env="sandbox", part_id="docpart_rt1", limit=2))[
        "data"
    ]
    assert [row["id"] for row in first["items"]] == ["strow_1", "strow_2"]
    assert first["result_schema_id"] == "assaysch_abc123"
    assert first["next_token"]
    second = (
        await call(
            client,
            "results_rows",
            env="sandbox",
            part_id="docpart_rt1",
            limit=2,
            cursor=first["next_token"],
        )
    )["data"]
    assert [row["id"] for row in second["items"]] == ["strow_3"]
    assert second["next_token"] is None
    assert fake.writes() == []


async def test_results_rows_tool_maps_a_listed_table_id(client, fake) -> None:
    fake.add_results_table()
    fake.parts.append(
        {
            "id": "docpart_rt1",
            "__typename": "DocumentPartResultsTable",
            "name": "Plate",
            "resultSchema": {"__typename": "ResultSchemaRef", "id": "assaysch_abc123"},
        }
    )
    mapped = await call(
        client, "results_rows", env="sandbox", part_id="strtbl_example", entry=ENTRY_ID
    )
    assert mapped["data"]["id"] == "docpart_rt1"
    assert mapped["data"]["table_id"] == "strtbl_example"
    refused = await call(client, "results_rows", env="sandbox", part_id="strtbl_example")
    assert refused["ok"] is False
    assert refused["error"]["code"] == "validation_error"
    assert refused["error"]["details"]["expected_id_namespace"] == "v3_document_part"
    assert fake.writes() == []


async def test_tool_descriptions_name_id_namespaces(client) -> None:
    tools = {tool.name: tool for tool in (await client.list_tools()).tools}
    expected = {
        "results_get": ["result_", "UUID"],
        "results_list": ["result_"],
        "results_tables": ["strtbl_", "docpart_"],
        "results_rows": ["docpart_", "strtbl_"],
        "blob_get": ["UUID", "attachment_"],
    }
    for name, tokens in expected.items():
        text = json.dumps(
            {"description": tools[name].description, "schema": tools[name].inputSchema}
        )
        for token in tokens:
            assert token in text, (name, token)


async def test_read_back_and_verify_results_without_allow_writes(client, pool, fake) -> None:
    runtime = pool.get("sandbox")
    planned = await runtime.planner.plan(
        ENTRY_ID,
        "results",
        {
            "table_id": "strtbl_example",
            "assay_results": [{"schemaId": "assaysch_abc123", "fields": {"ct": {"value": 3}}}],
        },
    )
    await runtime.planner.apply(planned.id)
    (result_id,) = fake.complete_upload()
    writes = len(fake.writes())

    pending = (await call(client, "results_pending", env="sandbox", plan_id=planned.id))["data"]
    assert pending["needs_accept"] == [result_id]
    assert "plug" in pending["next_step"]
    verified = (await call(client, "results_verify", env="sandbox", plan_id=planned.id))["data"]
    assert verified["ok"] is True and verified["counts"]["pending"] == 1
    assert len(fake.writes()) == writes


async def test_list_schemas_pages_without_allow_writes(client, fake) -> None:
    fake.schema_page_size = 2
    first = (await call(client, "schema_list", env="sandbox", limit=2))["data"]
    assert [item["kind"] for item in first["items"]] == ["entry", "entry"]
    assert first["next_token"]
    second = (
        await call(client, "schema_list", env="sandbox", limit=50, cursor=first["next_token"])
    )["data"]
    ids = [item["id"] for item in first["items"] + second["items"]]
    assert ids[0] == "entsch_abc123"
    assert "runsch_two" in ids
    assert len(ids) == len(set(ids))
    assert second["next_token"] is None
    assert fake.writes() == []


async def test_list_schemas_accepts_an_exact_name(client, fake) -> None:
    data = (await call(client, "schema_list", env="sandbox", name="Second result schema"))["data"]
    assert [item["id"] for item in data["items"]] == ["assaysch_two"]
    assert fake.writes() == []


async def test_find_schema_returns_typed_fields(client, fake) -> None:
    data = (
        await call(
            client,
            "schema_find",
            env="sandbox",
            name="Schema assaysch_abc123",
            kind="result",
        )
    )["data"]
    fields = {field["systemName"]: field for field in data["field_definitions"]}
    assert fields["ct"]["type"] == "INTEGER"
    assert fields["status"]["dropdown_options"][0]["id"] == "sfso_open"
    assert fake.writes() == []


async def test_get_schema_includes_dropdown_options(client, fake) -> None:
    data = (await call(client, "schema_get", env="sandbox", schema_id="entsch_choice"))["data"]
    status = data["field_definitions"][0]
    assert status["dropdown"]["name"] == "Status"
    assert status["dropdown"]["options"][0]["id"] == "sfso_open"
    orphan = data["field_definitions"][2]
    assert "dropdown" not in orphan
    assert fake.writes() == []


def test_serve_runs_stdio_transport_with_validated_writable_envs(
    benchling_env, monkeypatch
) -> None:
    from mcp.server.fastmcp import FastMCP

    seen: list[str] = []
    monkeypatch.setattr(FastMCP, "run", lambda self, transport: seen.append(transport))
    run_mcp_server(writable_envs={"sandbox"})
    assert seen == ["stdio"]


def test_closed_loop_on_shutdown_is_not_a_startup_failure(benchling_env, monkeypatch) -> None:
    from mcp.server.fastmcp import FastMCP

    from benchling_tools.mcp_server import _close_pool, _closed_event_loop

    monkeypatch.setattr(
        FastMCP,
        "run",
        lambda self, transport: (_ for _ in ()).throw(RuntimeError("Event loop is closed")),
    )
    run_mcp_server()

    def close_raises(coro: object) -> None:
        close = getattr(coro, "close", None)
        if close is not None:
            close()
        raise RuntimeError("Event loop is closed")

    monkeypatch.setattr(FastMCP, "run", lambda self, transport: None)
    monkeypatch.setattr("benchling_tools.mcp_server.asyncio.run", close_raises)
    run_mcp_server()

    grouped = BaseExceptionGroup("shutdown", [RuntimeError("Event loop is closed")])
    assert _closed_event_loop(grouped) is True
    assert _closed_event_loop(RuntimeError("boom")) is False
    pool = RuntimePool()
    _close_pool(pool)


def test_stdin_close_after_a_tool_call_exits_quietly(tmp_path: object) -> None:
    import os
    import subprocess
    import sys

    script = """
import keyring
from memory_keyring import MemoryKeyring

keyring.set_keyring(MemoryKeyring())
from benchling_tools.environment import EnvironmentConfigStore
from benchling_tools.keychain import Keychain
from benchling_tools.models import AuthMethod, Environment
from benchling_tools.services import BenchlingService

async def fake_resolve(self, reference):
    try:
        await self.api.http.get("http://127.0.0.1:9", timeout=0.2)
    except Exception:
        pass
    return {"entry_id": "etr_abc123"}

BenchlingService.resolve_entry = fake_resolve
EnvironmentConfigStore().upsert(
    Environment(
        name="sandbox",
        tenant_url="https://your-tenant.benchling.com",
        auth_method=AuthMethod.CLIENT_CREDENTIALS,
        client_id="client",
    )
)
Keychain().set("sandbox", "sandbox-client-secret")
from benchling_tools.cli import app
app(["mcp", "serve"])
"""
    # The child is the server. The parent sends the session and closes stdin.
    env = os.environ.copy()
    env["PYTHONPATH"] = "tests" + os.pathsep + env.get("PYTHONPATH", "")
    env["BENCHLING_TOOLS_HOME"] = str(tmp_path)
    proc = subprocess.Popen(
        [sys.executable, "-c", script],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
    )
    assert proc.stdin is not None and proc.stdout is not None and proc.stderr is not None
    initialize = json.dumps(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": "test", "version": "0"},
            },
        }
    )
    proc.stdin.write((initialize + "\n").encode())
    proc.stdin.flush()
    assert proc.stdout.readline()
    proc.stdin.write(
        (json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}) + "\n").encode()
    )
    call = json.dumps(
        {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {
                "name": "entry_resolve",
                "arguments": {"env": "sandbox", "reference": "etr_abc123"},
            },
        }
    )
    proc.stdin.write((call + "\n").encode())
    proc.stdin.flush()
    assert b"etr_abc123" in proc.stdout.readline()
    proc.stdin.close()
    stderr = proc.stderr.read()
    code = proc.wait(timeout=8)
    assert code == 0
    assert b"MCP server failed to start" not in stderr
    assert b"Event loop is closed" not in stderr


async def test_plan_changes_updates_results_from_a_csv(writer, fake, tmp_path) -> None:
    fake.results = [
        {
            "id": "result_1",
            "modifiedAt": "2026-01-02T00:00:00Z",
            "entry": {"id": ENTRY_ID},
            "schema": {"id": "assaysch_abc123"},
            "schemaFields": [
                {
                    "fieldDefinition": {"id": "assayfield_ct"},
                    "value": {"__typename": "IntegerValue", "value": 12},
                }
            ],
        }
    ]
    path = tmp_path / "assay.csv"
    path.write_text("id,Ct\nresult_1,15\n", encoding="utf-8")
    planned = await call(
        writer,
        "changes_plan",
        env="sandbox",
        entry=ENTRY_ID,
        family="results",
        spec={
            "operation": "update",
            "csv_path": str(path),
            "id_column": "id",
            "column_map": {"Ct": "Ct"},
        },
    )
    assert planned["ok"] is True
    assert fake.writes() == []
    assert planned["data"]["after"]["results"][0]["schema_fields"]["assayfield_ct"]["value"] == 15
    applied = await call(writer, "changes_apply", env="sandbox", plan_id=planned["data"]["id"])
    assert applied["ok"] is True
    assert applied["data"]["journal"]["result"]["state"] == "updated"
    path.write_text("id,Ct\nresult_1,16\n", encoding="utf-8")
    rejected = await call(
        writer,
        "changes_plan",
        env="sandbox",
        entry=ENTRY_ID,
        family="results",
        spec={
            "operation": "update",
            "csv_path": str(path),
            "id_column": "id",
            "column_map": {"Missing": "Missing"},
        },
    )
    assert rejected["ok"] is False
    assert rejected["error"]["code"] == "validation_error"
    assert "not in the header" in rejected["error"]["message"]


def _property_schemas(schema):
    """Yield every named property schema, including nested change-spec fields."""
    if not isinstance(schema, dict):
        return
    properties = schema.get("properties")
    if isinstance(properties, dict):
        for name, prop in properties.items():
            if isinstance(prop, dict):
                yield name, prop
                yield from _property_schemas(prop)
    for key in ("anyOf", "oneOf", "allOf"):
        for item in schema.get(key) or []:
            yield from _property_schemas(item)
    items = schema.get("items")
    if isinstance(items, dict):
        yield from _property_schemas(items)
    additional = schema.get("additionalProperties")
    if isinstance(additional, dict):
        yield from _property_schemas(additional)


async def test_every_tool_parameter_has_a_description(client) -> None:
    tools = (await client.list_tools()).tools
    assert tools
    for tool in tools:
        properties = tool.inputSchema.get("properties", {})
        for name, prop in properties.items():
            description = prop.get("description")
            assert isinstance(description, str) and description.strip(), (tool.name, name)
    plan = next(tool for tool in tools if tool.name == "changes_plan")
    specification = plan.inputSchema["properties"]["spec"]
    assert "anyOf" in specification
    assert specification.get("additionalProperties") is not True
    encoded = json.dumps(specification)
    assert "$ref" not in encoded
    for token in (
        "entry_template_id",
        "folder_id",
        "insert",
        "delete",
        "move",
        "replace_text",
        "set_cells",
        "append_rows",
        "schema_fields",
        "assay_results",
        "batches",
        "csv_path",
        "id_column",
        "column_map",
        "source_container_id",
        "destination_container_id",
        "destination_contents",
    ):
        assert token in encoded
    described = list(_property_schemas(specification))
    assert described
    for name, prop in described:
        description = prop.get("description")
        assert isinstance(description, str) and description.strip(), name
    family = plan.inputSchema["$defs"]["PlanFamily"]
    assert set(family["enum"]) == {
        "entry",
        "document",
        "table",
        "fields",
        "results",
        "containers",
    }


async def test_invalid_plan_specification_is_an_envelope(client, fake) -> None:
    cases = (
        ("nope", "must be an object"),
        ({"operation": "explode"}, "supported shape"),
        ({"operation": "delete", "from_part_id": 7}, "from_part_id"),
        (
            {"operation": "update", "id_column": "id", "column_map": {"Ct": "Ct"}},
            "csv_path",
        ),
    )
    for specification, expected in cases:
        payload = await call(
            client,
            "changes_plan",
            env="sandbox",
            entry=ENTRY_ID,
            family="document",
            spec=specification,
        )
        assert payload["ok"] is False
        assert payload["error"]["code"] == "validation_error"
        assert expected in payload["error"]["message"]
        assert payload["meta"]["env"] == "sandbox"
    assert fake.calls == []
    assert fake.writes() == []


async def test_plan_changes_accepts_inline_records_and_schema_fields(client, fake) -> None:
    uploaded = await call(
        client,
        "changes_plan",
        env="sandbox",
        entry=ENTRY_ID,
        family="results",
        spec={
            "table_id": "strtbl_example",
            "assay_results": [{"schemaId": "assaysch_abc123", "fields": {"ct": {"value": 12}}}],
        },
    )
    assert uploaded["ok"] is True
    record = uploaded["data"]["request"]["assay_results"][0]
    assert set(record) == {"schemaId", "fields"}
    assert uploaded["data"]["family"] == "results"
    fields = await call(
        client,
        "changes_plan",
        env="sandbox",
        entry=ENTRY_ID,
        family="fields",
        spec={"schema_fields": {"notes": {"value": "free text"}}},
    )
    assert fields["ok"] is True
    assert fields["data"]["family"] == "fields"
    assert fake.writes() == []


async def test_apply_changes_for_results_returns_the_next_step(writer, fake) -> None:
    planned = await call(
        writer,
        "changes_plan",
        env="sandbox",
        entry=ENTRY_ID,
        family="results",
        spec={
            "table_id": "strtbl_example",
            "assay_results": [{"schemaId": "assaysch_abc123", "fields": {"ct": {"value": 12}}}],
        },
    )
    assert planned["ok"] is True, planned
    applied = await call(writer, "changes_apply", env="sandbox", plan_id=planned["data"]["id"])
    assert applied["ok"] is True
    assert "pending" in applied["data"]["next_step"]
    assert "Assay results" in applied["data"]["next_step"]
    fake.task = {"status": "SUCCEEDED", "response": {"assayResults": []}}
    status = await call(
        writer,
        "changes_status",
        env="sandbox",
        plan_id=planned["data"]["id"],
        wait=True,
        timeout=1,
    )
    assert status["data"]["result_state"] == "pending_integration"


async def test_entry_tools_return_web_url(client) -> None:
    url = entry_web_url(ENTRY_ID)
    resolved = (await call(client, "entry_resolve", env="sandbox", reference=ENTRY_ID))["data"]
    entry = (await call(client, "entry_get", env="sandbox", reference=ENTRY_ID))["data"]
    full = (await call(client, "entry_get", env="sandbox", reference=ENTRY_ID, full=True))["data"]
    listed = (await call(client, "entry_list", env="sandbox"))["data"]
    bulk = (await call(client, "entry_bulk_get", env="sandbox", entry_ids=[ENTRY_ID]))["data"]
    assert resolved["web_url"] == entry["web_url"] == full["web_url"] == url
    assert listed["items"][0]["web_url"] == url
    assert bulk["items"][0]["web_url"] == url


async def test_entry_read_tools_describe_web_url(client) -> None:
    tools = {tool.name: tool for tool in (await client.list_tools()).tools}
    for name in ("entry_resolve", "entry_get", "entry_list", "entry_bulk_get"):
        assert "web_url" in (tools[name].description or ""), name


CLI_ONLY_COMMANDS = {
    ("auth", "login"),
    ("auth", "logout"),
    ("auth", "protect"),
    ("auth", "unprotect"),
    ("skill", "path"),
    ("skill", "show"),
    ("mcp", "serve"),
}


def _cli_commands():
    import typer

    from benchling_tools import cli
    from benchling_tools.mcp_server import cli_commands

    return dict(cli_commands(typer.main.get_command(cli.app)))


async def test_every_cli_command_is_one_tool_named_after_its_path(client) -> None:
    from benchling_tools.commands import CLI_ONLY_ATTR, TOOL_ATTR

    commands = _cli_commands()
    cli_only = {
        path for path, command in commands.items() if hasattr(command.callback, CLI_ONLY_ATTR)
    }
    declared = {path for path, command in commands.items() if hasattr(command.callback, TOOL_ATTR)}
    assert cli_only == CLI_ONLY_COMMANDS
    assert declared | cli_only == set(commands)
    expected = {"_".join(path).replace("-", "_") for path in declared}
    tools = {tool.name for tool in (await client.list_tools()).tools}
    assert tools == expected
    assert tools == READ_TOOLS | {
        "blob_download",
        "export",
        "issue_draft",
        "changes_plan",
        "changes_apply",
        "changes_export",
    }


def _tool_text(text: str, command) -> str:
    names = {"--env": "env"}
    for param in command.params:
        names.update({option: param.name for option in param.opts if option.startswith("--")})
    return re.sub(r"--[a-z][a-z-]*", lambda m: names.get(m.group(0), m.group(0)), text)


async def test_tool_text_comes_from_cli_help(client) -> None:
    from benchling_tools.commands import TOOL_ATTR

    tools = {tool.name: tool for tool in (await client.list_tools()).tools}
    for path, command in _cli_commands().items():
        declared = getattr(command.callback, TOOL_ATTR, None)
        if declared is None:
            continue
        tool = tools["_".join(path).replace("-", "_")]
        summary = declared.doc.split("\n")[0]
        assert tool.description.split("\n")[0] == _tool_text(summary, command), path
        properties = tool.inputSchema.get("properties", {})
        for param in command.params:
            if param.name == "env" and declared.needs_env:
                assert "env" in tool.inputSchema["required"], path
                continue
            if param.name in declared.cli_only or param.name in declared.mcp_params:
                assert param.name not in properties or param.name in declared.mcp_params
                continue
            assert properties[param.name]["description"] == _tool_text(param.help, command), (
                path,
                param.name,
            )
            if getattr(param.type, "min", None) is not None:
                assert properties[param.name]["minimum"] == param.type.min
        for param in command.params:
            for option in param.opts:
                if option.startswith("--"):
                    assert option not in tool.description, (path, option)
    apply = tools["changes_apply"]
    assert "confirm_env" not in apply.inputSchema.get("required", [])
    assert "person has confirmed" in apply.inputSchema["properties"]["confirm_env"]["description"]
    assert "only after the person has confirmed" in apply.description
    assert "spec_json" not in tools["changes_plan"].inputSchema["properties"]


def test_a_command_without_a_declaration_fails_the_build(pool, monkeypatch) -> None:
    import typer

    from benchling_tools import cli

    extra = typer.Typer()

    @extra.command("loose")
    def loose() -> None:
        """Not declared."""

    monkeypatch.setattr(cli, "app", typer.Typer())
    cli.app.add_typer(extra, name="extra")
    with pytest.raises(TypeError, match="extra loose"):
        build_mcp_server(pool)


@pytest.mark.parametrize(
    ("arguments", "message"),
    [
        (
            {
                "family": "entry",
                "entry": ENTRY_ID,
                "spec": {
                    "entry_template_id": "etpl_abc123",
                    "folder_id": "lib_abc123",
                    "name": "X",
                },
            },
            "must not specify entry",
        ),
        (
            {"family": "document", "spec": {"operation": "delete", "from_part_id": TEXT_PART}},
            "requires entry",
        ),
        (
            {"family": "results", "entry": ENTRY_ID, "column_map": ["a=b"]},
            "id_column and column_map require csv_path",
        ),
    ],
)
async def test_plan_entry_rules_are_validation_envelopes(client, fake, arguments, message) -> None:
    payload = await call(client, "changes_plan", env="sandbox", **arguments)
    assert payload["error"]["code"] == "validation_error"
    assert message in payload["error"]["message"]
    assert fake.writes() == []


async def test_every_tool_description_ends_with_its_write_class(client) -> None:
    from benchling_tools.mcp_server import WRITE_CLASS_NOTES

    tools = {tool.name: tool for tool in (await client.list_tools()).tools}
    expected = {name: "read" for name in READ_TOOLS | {"issue_draft"}}
    expected.update(
        blob_download="local",
        export="local",
        changes_plan="local",
        changes_export="local",
        changes_apply="benchling",
    )
    assert set(expected) == set(tools)
    for name, tool in tools.items():
        note = tool.description.rsplit("\n\n", 1)[-1]
        assert note == WRITE_CLASS_NOTES[expected[name]], name
    assert "reads only" in tools["entry_get"].description
    assert "writes a local file or plan" in tools["export"].description
    assert "writes a local file or plan" in tools["changes_export"].description
    assert "writes to Benchling" in tools["changes_apply"].description
    assert "--allow-writes for env" in tools["changes_apply"].description
