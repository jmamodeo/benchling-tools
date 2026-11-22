from __future__ import annotations

import json
import os

import pytest
import typer
from conftest import configure_env, plain_stdout, runtime_for
from fake_benchling import ENTRY_ID, TABLE_PART, TEXT_PART, entry_web_url
from typer.testing import CliRunner

from benchlingtk import cli
from benchlingtk.errors import ExitCode


@pytest.fixture
def invoke(benchling_env, fake, monkeypatch):
    monkeypatch.setattr(cli, "Runtime", lambda environment: runtime_for(fake, environment))

    def run(*args: str, input: str | None = None, env: str | None = "sandbox"):
        suffix = ["--env", env] if env else []
        result = CliRunner().invoke(cli.app, [*args, *suffix], input=input)
        return result.exit_code, json.loads(result.stdout), result

    return run


def test_envelope_shape_and_env_metadata_on_success(invoke) -> None:
    code, payload, _ = invoke("entry", "resolve", ENTRY_ID)
    assert code == 0
    assert payload["ok"] is True and payload["error"] is None
    assert payload["meta"] == {
        "env": "sandbox",
        "tenant_url": "https://your-tenant-dev.benchling.com",
    }
    assert payload["data"]["entry_id"] == ENTRY_ID


def test_entry_creation_plan_uses_optional_entry_and_schema_fields(invoke, fake, tmp_path) -> None:
    request = {
        "entry_template_id": "etpl_abc123",
        "folder_id": "lib_abc123",
        "name": "Fresh experiment",
        "schema_fields": {"temperature": {"value": 21}},
    }
    spec = tmp_path / "entry-create.json"
    spec.write_text(json.dumps(request), encoding="utf-8")

    code, planned, _ = invoke("changes", "plan", "--family", "entry", "--spec", str(spec))

    assert code == 0
    assert planned["data"]["request"] == request
    assert planned["data"]["after"]["request"]["schemaFields"] == {"tsf_temp": {"value": 21}}
    assert fake.writes() == []


def test_environment_is_required_for_api_commands(invoke, fake) -> None:
    code, payload, _ = invoke("doctor", env=None)
    assert code == 2
    assert payload["error"]["code"] == "validation_error"
    assert payload["error"]["details"]["available_envs"] == ["sandbox"]
    assert fake.calls == []


def test_unknown_environment_is_rejected_before_any_request(invoke, fake) -> None:
    code, payload, _ = invoke("doctor", env="prod")
    assert code == 2
    assert payload["error"]["details"]["env"] == "prod"
    assert fake.calls == []


def test_doctor_reports_capabilities(invoke) -> None:
    code, payload, _ = invoke("doctor")
    assert code == 0
    assert payload["ok"] is True
    assert payload["data"]["ok"] is True
    assert [c["name"] for c in payload["data"]["checks"]] == ["v3_entries", "v2_entries"]


def test_doctor_fails_the_envelope_when_checks_fail(invoke, fake) -> None:
    fake.fail("GET", "/api/v3/entry/items", 401)
    fake.fail("GET", "/api/v2/entries", 403)
    code, payload, _ = invoke("doctor")
    assert code == 3
    assert payload["ok"] is False
    assert payload["error"]["code"] == "authentication_error"
    checks = payload["error"]["details"]["checks"]
    assert all(check["hint"] for check in checks)
    assert "client ID and secret" in checks[0]["hint"]
    assert "allowed to read" in checks[1]["hint"]


def test_entry_get_full_and_summary(invoke) -> None:
    code, summary, _ = invoke("entry", "get", "EXP-1")
    assert code == 0 and summary["data"]["version_id"] == "ver_1"
    assert "parts" not in summary["data"]
    code, full, _ = invoke("entry", "get", ENTRY_ID, "--full")
    assert code == 0
    assert len(full["data"]["parts"]) == 6
    assert full["data"]["completeness"] == "partial"


def test_version_flag_matches_package_metadata(invoke) -> None:
    from importlib.metadata import version

    result = CliRunner().invoke(cli.app, ["--version"])
    assert result.exit_code == 0
    assert result.stdout.strip() == f"benchlingtk {version('benchlingtk')}"


def test_distribution_and_command_are_named_benchlingtk() -> None:
    from importlib.metadata import distribution

    entry_points = distribution("benchlingtk").entry_points.select(group="console_scripts")
    assert {ep.name: ep.value for ep in entry_points} == {"benchlingtk": "benchlingtk.cli:main"}
    assert cli.app.info.name == "benchlingtk"


@pytest.mark.parametrize(
    ("args", "key", "expected"),
    [
        (["entry", "inspect", ENTRY_ID], "version_id", "ver_1"),
        (["entry", "fields", "get", ENTRY_ID], "entry_id", ENTRY_ID),
        (["section", "get", ENTRY_ID, "Results"], "version_id", "ver_1"),
        (["part", "get", ENTRY_ID, TEXT_PART], "entry_id", ENTRY_ID),
        (["table", "get", TABLE_PART], "versionMetadata", {"versionId": "ver_1"}),
        (
            ["schema", "get", "entsch_abc123"],
            "schema",
            {"id": "entsch_abc123", "name": "Schema entsch_abc123"},
        ),
        (["template", "get", "etpl_abc123"], "template_id", "etpl_abc123"),
        (["folder", "get", "lib_abc123"], "id", "lib_abc123"),
        (["results", "get", "result_1"], "id", "result_1"),
        (["results", "tables", ENTRY_ID], "complete", True),
        (["container", "get", "con_source1"], "id", "con_source1"),
        (
            ["container", "find", "--barcode", "BAR-SOURCE"],
            "items",
            [
                {
                    "id": "con_source1",
                    "name": "Source vial",
                    "barcode": "BAR-SOURCE",
                    "modifiedAt": "2026-01-01T00:00:00Z",
                    "schema": {"id": "contsch_abc123"},
                    "contents": [{"entity": {"id": "ent_sample1"}}],
                }
            ],
        ),
        (
            ["container", "schema", "list"],
            "items",
            [
                {
                    "id": "contsch_abc123",
                    "name": "Synthetic vial schema",
                    "type": "container",
                    "modifiedAt": "2026-01-01T00:00:00Z",
                    "fieldDefinitions": [{"name": "Storage note", "type": "text"}],
                }
            ],
        ),
    ],
)
def test_read_commands_succeed(invoke, args, key, expected) -> None:
    code, payload, _ = invoke(*args)
    assert code == 0, payload
    assert payload["data"][key] == expected


def test_entry_bulk_get_accepts_ids_or_display_ids(invoke, fake) -> None:
    code, payload, _ = invoke("entry", "bulk-get", "--id", ENTRY_ID, "--id", "etr_list2")
    assert code == 0
    assert [item["id"] for item in payload["data"]["items"]] == [ENTRY_ID, "etr_list2"]
    (call,) = [item for item in fake.calls if item.path.endswith("/entries:bulk-get")]
    assert call.params == {"entryIds": f"{ENTRY_ID},etr_list2"}

    code, payload, _ = invoke(
        "entry",
        "bulk-get",
        "--display-id",
        "EXP-1",
        "--display-id",
        "EXP-2",
    )
    assert code == 0
    assert [item["displayId"] for item in payload["data"]["items"]] == ["EXP-1", "EXP-2"]


def test_template_list_forwards_name_and_cursor(invoke, fake) -> None:
    code, payload, _ = invoke("template", "list", "--name", "Synthetic template", "--limit", "10")
    assert code == 0
    assert payload["data"]["items"][0]["id"] == "etpl_abc123"
    (call,) = [item for item in fake.calls if item.path.endswith("/entry-templates")]
    assert call.params == {"pageSize": "10", "name": "Synthetic template"}


def test_results_list_forwards_filters_and_cursor(invoke, fake) -> None:
    fake.results_page_size = 2
    code, payload, _ = invoke(
        "results",
        "list",
        "--entry",
        ENTRY_ID,
        "--schema",
        "assaysch_abc123",
        "--page-size",
        "2",
        "--cursor",
        "1",
    )
    assert code == 0
    assert [r["id"] for r in payload["data"]["items"]] == ["result_2", "result_3"]
    (call,) = [c for c in fake.calls if c.path.endswith("/result/items")]
    assert call.params == {
        "pageSize": "2",
        "nextToken": "1",
        "entry.eq": ENTRY_ID,
        "schema.eq": "assaysch_abc123",
    }


@pytest.mark.parametrize("size", ["0", "101"])
def test_results_list_page_size_bounds_are_enforced_by_the_cli(invoke, fake, size) -> None:
    result = CliRunner().invoke(
        cli.app, ["results", "list", "--page-size", size, "--env", "sandbox"]
    )
    assert result.exit_code == ExitCode.USAGE
    assert json.loads(result.stdout)["error"]["code"] == "usage_error"
    assert fake.calls == []


@pytest.mark.parametrize(
    ("status", "code", "error"),
    [
        (401, 3, "authentication_error"),
        (403, 4, "access_denied"),
        (404, 4, "not_found"),
        (400, 2, "validation_error"),
        (409, 5, "conflict"),
        (0, 7, "upstream_error"),
    ],
)
def test_error_codes_map_to_exit_codes(invoke, fake, status, code, error) -> None:
    fake.fail("GET", f"/entry/{ENTRY_ID}", status)
    exit_code, payload, _ = invoke("entry", "resolve", ENTRY_ID)
    assert exit_code == code
    assert payload["ok"] is False and payload["data"] is None
    assert payload["error"]["code"] == error


def test_missing_entry_by_display_id_exits_not_found(invoke) -> None:
    code, payload, _ = invoke("entry", "resolve", "EXP-404")
    assert code == 4
    assert payload["error"]["details"] == {"reference": "EXP-404"}


def test_url_from_another_tenant_is_rejected(invoke, fake) -> None:
    code, payload, _ = invoke(
        "entry", "resolve", f"https://someone-else.benchling.com/s/x/{ENTRY_ID}"
    )
    assert code == 2
    assert "different tenant" in payload["error"]["message"]
    assert fake.calls == []


def test_same_tenant_url_resolves(invoke) -> None:
    code, payload, _ = invoke(
        "entry", "resolve", f"https://your-tenant-dev.benchling.com/s/lib/{ENTRY_ID}-title/edit"
    )
    assert code == 0
    assert payload["data"]["entry_id"] == ENTRY_ID


def write_spec(tmp_path, spec) -> str:
    path = tmp_path / "spec.json"
    path.write_text(spec if isinstance(spec, str) else json.dumps(spec))
    return str(path)


DELETE_SPEC = {"operation": "delete", "from_part_id": TEXT_PART}


def test_plan_status_apply_lifecycle_through_cli(invoke, fake, tmp_path) -> None:
    code, planned, _ = invoke(
        "changes",
        "plan",
        "--entry",
        ENTRY_ID,
        "--family",
        "document",
        "--spec",
        write_spec(tmp_path, DELETE_SPEC),
    )
    assert code == 0
    plan_id = planned["data"]["id"]
    assert fake.writes() == []
    _, status, _ = invoke("changes", "status", plan_id)
    assert status["data"]["journal"]["status"] == "pending"
    code, applied, _ = invoke("changes", "apply", plan_id)
    assert code == 0 and applied["data"]["journal"]["status"] == "succeeded"
    assert TEXT_PART not in fake.part_ids()
    code, again, _ = invoke("changes", "apply", plan_id)
    assert code == 5 and again["error"]["code"] == "conflict"


def test_container_create_uses_changes_plan_and_apply(invoke, fake, tmp_path) -> None:
    code, planned, _ = invoke(
        "changes",
        "plan",
        "--family",
        "containers",
        "--spec",
        write_spec(
            tmp_path,
            {
                "operation": "create",
                "schema_id": "contsch_abc123",
                "name": "Synthetic vial from CLI",
            },
        ),
    )
    assert code == 0
    assert planned["data"]["entry_id"] is None
    assert planned["data"]["family"] == "containers"
    assert fake.writes() == []
    code, applied, _ = invoke("changes", "apply", planned["data"]["id"])
    assert code == 0
    assert applied["data"]["journal"]["status"] == "succeeded"
    assert [call.path for call in fake.writes()] == ["/api/v2/containers"]


def test_results_status_wait_is_available_through_cli(invoke, fake, tmp_path) -> None:
    code, planned, _ = invoke(
        "changes",
        "plan",
        "--entry",
        ENTRY_ID,
        "--family",
        "results",
        "--spec",
        write_spec(
            tmp_path,
            {
                "table_id": "strtbl_example",
                "assay_results": [{"schemaId": "assaysch_abc123", "fields": {"ct": {"value": 12}}}],
            },
        ),
    )
    assert code == 0, planned
    code, applied, _ = invoke("changes", "apply", planned["data"]["id"])
    assert code == 0, applied
    fake.task = {"status": "SUCCEEDED", "response": {"assayResults": []}}
    code, status, _ = invoke("changes", "status", planned["data"]["id"], "--wait", "--timeout", "1")
    assert code == 0, status
    assert status["data"]["result_state"] == "pending_integration"
    assert status["data"]["tasks"][0]["task_status"] == "SUCCEEDED"


def test_plan_reads_spec_from_stdin(invoke) -> None:
    code, planned, _ = invoke(
        "changes",
        "plan",
        "--entry",
        ENTRY_ID,
        "--family",
        "document",
        "--spec",
        "-",
        input=json.dumps(DELETE_SPEC),
    )
    assert code == 0
    assert planned["data"]["request"] == DELETE_SPEC


@pytest.mark.parametrize("content", ["not json", "[1, 2]", '"text"'])
def test_plan_rejects_bad_spec_content(invoke, fake, tmp_path, content) -> None:
    code, payload, _ = invoke(
        "changes",
        "plan",
        "--entry",
        ENTRY_ID,
        "--family",
        "document",
        "--spec",
        write_spec(tmp_path, content),
    )
    assert code == 2
    assert payload["error"]["code"] == "validation_error"
    assert fake.calls == []


def test_plan_rejects_missing_spec_file(invoke, tmp_path) -> None:
    code, payload, _ = invoke(
        "changes",
        "plan",
        "--entry",
        ENTRY_ID,
        "--family",
        "document",
        "--spec",
        str(tmp_path / "missing.json"),
    )
    assert code == 2
    assert payload["error"]["details"]["path"].endswith("missing.json")


def test_plan_family_is_validated_case_insensitively(invoke, tmp_path) -> None:
    code, _, _ = invoke(
        "changes",
        "plan",
        "--entry",
        ENTRY_ID,
        "--family",
        "DOCUMENT",
        "--spec",
        write_spec(tmp_path, DELETE_SPEC),
    )
    assert code == 0
    result = CliRunner().invoke(
        cli.app,
        [
            "changes",
            "plan",
            "--entry",
            ENTRY_ID,
            "--family",
            "magic",
            "--spec",
            "-",
            "--env",
            "sandbox",
        ],
    )
    assert result.exit_code == ExitCode.USAGE
    assert json.loads(result.stdout)["error"]["code"] == "usage_error"


def test_apply_with_unconfirmed_write_exits_with_partial_code(invoke, fake, tmp_path) -> None:
    _, planned, _ = invoke(
        "changes",
        "plan",
        "--entry",
        ENTRY_ID,
        "--family",
        "document",
        "--spec",
        write_spec(tmp_path, DELETE_SPEC),
    )
    fake.fail("PATCH", f"/entry/{ENTRY_ID}", 500)
    code, payload, _ = invoke("changes", "apply", planned["data"]["id"])
    assert code == 8
    assert payload["error"]["code"] == "partial_or_unknown_completion"
    assert payload["error"]["details"]["journal"]["status"] == "unknown"


def test_apply_rejects_malformed_plan_id(invoke) -> None:
    code, payload, _ = invoke("changes", "apply", "../etc/passwd")
    assert code == 2
    assert payload["error"]["code"] == "validation_error"


def test_auth_status_lists_environments_without_secrets_or_a_trailing_slash(invoke) -> None:
    code, payload, result = invoke("auth", "status", env=None)
    assert code == 0
    row = payload["data"]["environments"]["sandbox"]
    assert row["tenant_url"] == "https://your-tenant-dev.benchling.com"
    assert "early_access" not in row
    assert "sandbox-client-secret" not in result.stdout


def test_auth_status_is_an_empty_envelope_when_nothing_is_configured(monkeypatch) -> None:
    for name in list(os.environ):
        if name.startswith("BENCHLING_"):
            monkeypatch.delenv(name)
    result = CliRunner().invoke(cli.app, ["auth", "status"])
    assert result.exit_code == 0
    assert json.loads(result.stdout) == {
        "schema_version": "1.0",
        "ok": True,
        "data": {"environments": {}},
        "error": None,
        "meta": {},
    }


def test_envs_list_is_removed() -> None:
    result = CliRunner().invoke(cli.app, ["envs", "list"])
    assert result.exit_code != 0
    assert "No such command" in result.stdout + result.stderr


def test_doctor_names_login_when_the_secret_is_missing(invoke) -> None:
    from benchlingtk.keychain import Keychain

    Keychain().delete("sandbox")
    code, payload, _ = invoke("doctor")
    assert code == 3
    assert payload["ok"] is False
    assert (
        payload["error"]["message"]
        == "App client credentials are missing. Client secret is not in the keychain; "
        "run benchlingtk auth login --env sandbox"
    )
    assert payload["error"]["details"]["checks"][0]["hint"].endswith(
        "run benchlingtk auth login --env sandbox"
    )


def test_mcp_logging_is_quiet_unless_verbose(benchling_env) -> None:
    from benchlingtk.mcp_server import build_mcp_server
    from benchlingtk.runtime import RuntimePool

    quiet = build_mcp_server(RuntimePool())
    assert quiet.settings.log_level == "WARNING"
    loud = build_mcp_server(RuntimePool(), verbose=True)
    assert loud.settings.log_level == "DEBUG"
    help_text = plain_stdout(["mcp", "serve", "--help"])
    assert "--verbose" in help_text


def test_mcp_keychain_startup_error_is_json_on_stderr(monkeypatch) -> None:
    import keyring

    configure_env("dev", "https://your-tenant.benchling.com", secret="dev-client-secret")
    keyring.set_keyring(keyring.backends.fail.Keyring())
    monkeypatch.setattr("benchlingtk.keychain.provider_installed", lambda: True)
    monkeypatch.setattr("benchlingtk.keychain.secret_service_reachable", lambda: False)
    result = CliRunner().invoke(cli.app, ["mcp", "serve"])
    assert result.exit_code == 3
    assert result.stdout == ""
    payload = json.loads(result.stderr)
    message = payload["error"]["message"]
    assert message.count("\n") == 3
    assert "HOME, LOGNAME, PATH, SHELL, TERM and USER" in message
    assert "DBUS_SESSION_BUS_ADDRESS" in message
    assert "env block" in message
    assert "Cron needs that same long-lived bus exported." in message
    assert "dev-client-secret" not in result.stderr


def test_doctor_reports_an_unexpected_local_failure(monkeypatch) -> None:
    configure_env("dev", "https://your-tenant.benchling.com", secret="dev-client-secret")

    async def boom(self: object) -> dict[str, object]:
        raise RuntimeError("doctor-boom")

    monkeypatch.setattr("benchlingtk.services.BenchlingService.doctor", boom)
    result = CliRunner().invoke(cli.app, ["doctor", "--env", "dev"])
    assert result.exit_code == 7
    payload = json.loads(result.stdout)
    assert payload["ok"] is False
    assert payload["error"]["details"]["type"] == "RuntimeError"


def test_saved_tenant_url_must_be_text() -> None:
    from benchlingtk.environment import EnvironmentConfigStore

    store = EnvironmentConfigStore()
    store.ensure()
    store.environments_path.write_text(
        json.dumps(
            {"version": 1, "environments": {"dev": {"tenant_url": 1, "client_id": "dev-client"}}}
        ),
        encoding="utf-8",
    )
    result = CliRunner().invoke(cli.app, ["auth", "status", "--env", "dev"])
    assert result.exit_code == 2
    message = json.loads(result.stdout)["error"]["message"]
    assert "Invalid tenant URL" in message
    assert "1" not in message


def test_mcp_serve_forwards_verbose(monkeypatch) -> None:
    seen: dict[str, object] = {}

    def record(**kwargs: object) -> None:
        seen.update(kwargs)

    monkeypatch.setattr("benchlingtk.mcp_server.run_mcp_server", record)
    result = CliRunner().invoke(cli.app, ["mcp", "serve", "--verbose"])
    assert result.exit_code == 0
    assert seen["verbose"] is True


def test_mcp_unexpected_startup_error_stays_on_stderr(monkeypatch) -> None:
    def boom(**kwargs: object) -> None:
        raise RuntimeError("boom-start")

    monkeypatch.setattr("benchlingtk.mcp_server.run_mcp_server", boom)
    result = CliRunner().invoke(cli.app, ["mcp", "serve"])
    assert result.stdout == ""
    assert result.exit_code == 7
    payload = json.loads(result.stderr)
    assert payload["ok"] is False
    assert payload["error"]["details"]["reason"] == "boom-start"


def test_mcp_serve_reports_startup_errors_as_json_on_stderr(invoke, monkeypatch) -> None:
    from benchlingtk.keychain import Keychain

    unknown = CliRunner().invoke(cli.app, ["mcp", "serve", "--allow-writes", "prod"])
    assert unknown.exit_code == 2
    assert json.loads(unknown.stderr)["error"]["details"]["unknown_envs"] == ["prod"]

    Keychain().delete("sandbox")
    missing = CliRunner().invoke(cli.app, ["mcp", "serve"])
    assert missing.exit_code == 3
    assert missing.stdout == ""
    assert json.loads(missing.stderr)["error"]["code"] == "authentication_error"
    assert "sandbox-client-secret" not in missing.stderr


def test_auth_status_reports_without_calling_the_api(invoke, fake) -> None:
    code, payload, result = invoke("auth", "status")
    assert code == 0
    assert payload["meta"]["env"] == "sandbox"
    row = payload["data"]["environments"]["sandbox"]
    assert row["secret_present"] is True
    assert row["credential_source"] == "keyring"
    assert "sandbox-client-secret" not in result.stdout
    assert fake.calls == []


def test_invalid_saved_endpoint_fails_validation_without_echoing_value(invoke) -> None:
    from benchlingtk.environment import EnvironmentConfigStore

    path = EnvironmentConfigStore().environments_path
    saved = json.loads(path.read_text())
    saved["environments"]["sandbox"]["tenant_url"] = "http://insecure.example/"
    path.write_text(json.dumps(saved))
    code, payload, result = invoke("auth", "status")
    assert code == 2
    assert "insecure.example" not in result.stdout


def test_cli_without_arguments_shows_help() -> None:
    text = plain_stdout([])
    assert "Usage" in text and "changes" in text


def test_entry_list_forwards_filters_and_reports_server_side_routing(invoke, fake) -> None:
    code, payload, _ = invoke(
        "entry",
        "list",
        "--folder",
        "lib_other",
        "--schema",
        "entsch_abc123",
        "--limit",
        "10",
    )
    assert code == 0, payload
    assert [item["id"] for item in payload["data"]["items"]] == ["etr_list3"]
    assert payload["data"]["routing"]["api_version"] == 2
    assert payload["data"]["routing"]["client_side"] == []
    assert "folder" in payload["data"]["routing"]["server_side"]
    assert fake.writes() == []
    (call,) = [item for item in fake.calls if item.path.endswith("/entries")]
    assert call.params["folderId"] == "lib_other"
    assert call.params["schemaId"] == "entsch_abc123"
    assert "folderId" not in {
        key for item in fake.calls if item.path.endswith("/entry/items") for key in item.params
    }


def test_schema_list_pages_with_a_cursor(invoke, fake) -> None:
    fake.schema_page_size = 2
    code, first, _ = invoke("schema", "list", "--kind", "entry", "--limit", "2")
    assert code == 0, first
    assert [item["id"] for item in first["data"]["items"]] == ["entsch_abc123", "entsch_two"]
    assert first["data"]["next_token"]
    code, second, _ = invoke(
        "schema", "list", "--kind", "entry", "--limit", "2", "--cursor", first["data"]["next_token"]
    )
    assert code == 0
    assert [item["id"] for item in second["data"]["items"]] == ["entsch_three", "entsch_four"]
    assert fake.writes() == []


def test_schema_list_looks_up_an_exact_name(invoke, fake) -> None:
    code, payload, _ = invoke("schema", "list", "--name", "Second result schema")
    assert code == 0
    assert [item["id"] for item in payload["data"]["items"]] == ["assaysch_two"]
    assert fake.calls[0].params["name.anyOf.caseSensitive"] == "Second result schema"


def test_entry_list_sends_a_comma_name_as_the_v2_name_parameter(invoke, fake) -> None:
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
    code, payload, _ = invoke("entry", "list", "--name", "Assay, replicate 1")
    assert code == 0, payload
    assert payload["ok"] is True
    assert [item["id"] for item in payload["data"]["items"]] == ["etr_comma"]
    assert payload["data"]["items"][0]["modified_at"].endswith("Z")
    assert payload["data"]["routing"]["api_version"] == 2
    (call,) = [item for item in fake.calls if item.path.endswith("/entries")]
    assert call.params["name"] == "Assay, replicate 1"
    assert "names.anyOf" not in call.params
    assert fake.writes() == []


def test_entry_list_rejects_a_timestamp_without_a_zone(invoke, fake) -> None:
    code, payload, _ = invoke("entry", "list", "--modified-after", "2026-06-15T12:00:00")
    assert code == 2
    assert payload["error"]["code"] == "validation_error"
    assert "no time zone" in payload["error"]["message"]
    assert fake.calls == []


def test_results_rows_pages_with_the_standard_envelope(invoke, fake) -> None:
    fake.add_results_table()
    code, first, _ = invoke("results", "rows", "docpart_rt1", "--limit", "2")
    assert code == 0, first
    assert first["ok"] is True and first["error"] is None
    assert first["meta"]["env"] == "sandbox"
    assert [row["id"] for row in first["data"]["items"]] == ["strow_1", "strow_2"]
    assert first["data"]["column_definitions"][0]["columnId"] == "col_ct"
    assert first["data"]["next_token"]
    code, second, _ = invoke(
        "results",
        "rows",
        "docpart_rt1",
        "--limit",
        "2",
        "--cursor",
        first["data"]["next_token"],
    )
    assert code == 0, second
    assert [row["id"] for row in second["data"]["items"]] == ["strow_3"]
    assert second["data"]["next_token"] is None
    assert fake.writes() == []


@pytest.mark.parametrize("size", ["0", "101"])
def test_results_rows_limit_bounds_are_enforced_by_the_cli(invoke, fake, size) -> None:
    result = CliRunner().invoke(
        cli.app, ["results", "rows", "docpart_rt1", "--limit", size, "--env", "sandbox"]
    )
    assert result.exit_code == ExitCode.USAGE
    assert json.loads(result.stdout)["error"]["code"] == "usage_error"
    assert fake.calls == []


def test_list_commands_reject_bad_input_with_the_validation_exit_code(invoke, fake) -> None:
    code, payload, _ = invoke("entry", "list", "--modified-after", "yesterday")
    assert code == 2
    assert payload["error"]["code"] == "validation_error"
    code, payload, _ = invoke("schema", "list", "--kind", "sample")
    assert code == 2
    assert payload["error"]["code"] == "validation_error"
    assert fake.calls == []


@pytest.mark.parametrize("size", ["0", "101"])
def test_list_limit_bounds_are_enforced_by_the_cli(invoke, fake, size) -> None:
    result = CliRunner().invoke(cli.app, ["entry", "list", "--limit", size, "--env", "sandbox"])
    assert result.exit_code == ExitCode.USAGE
    assert json.loads(result.stdout)["error"]["code"] == "usage_error"
    assert fake.calls == []


def test_csv_result_plan_and_apply(invoke, fake, tmp_path) -> None:
    fake.results = [
        {
            "id": "result_1",
            "modifiedAt": "2026-01-02T00:00:00Z",
            "createdAt": "2026-01-01T00:00:00Z",
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
    path.write_text("id,createdAt,Ct\nresult_1,2026-01-01T00:00:00Z,15\n", encoding="utf-8")
    code, planned, _ = invoke(
        "changes",
        "plan",
        "--entry",
        ENTRY_ID,
        "--family",
        "results",
        "--csv",
        str(path),
        "--id-column",
        "id",
        "--map",
        "Ct=Ct",
    )
    assert code == 0, planned
    assert fake.writes() == []
    assert planned["data"]["after"]["results"][0]["schema_fields"] == {
        "assayfield_ct": {"value": 15}
    }
    assert planned["data"]["before"]["results"][0]["fields"]["ct"]["value"] == 12
    code, applied, _ = invoke("changes", "apply", planned["data"]["id"])
    assert code == 0, applied
    assert applied["data"]["journal"]["result"]["updated"][0]["http_status"] == 200
    assert fake.result_updates[0]["body"] == {"schemaFields": {"assayfield_ct": {"value": 15}}}


@pytest.mark.parametrize(
    ("extra", "match"),
    [
        ([], "needs --spec"),
        (["--spec", "spec.json", "--csv", "rows.csv"], "only one of"),
        (["--spec", "spec.json", "--spec-json", "{}"], "only one of"),
        (["--spec-json", "{}", "--csv", "rows.csv"], "only one of"),
        (["--spec-json", "{not json"], "Could not parse --spec-json"),
        (["--spec-json", "[1]"], "must be a JSON object"),
        (
            ["--family", "document", "--csv", "rows.csv", "--id-column", "id", "--map", "Ct=Ct"],
            "results family",
        ),
        (["--csv", "rows.csv", "--id-column", "id", "--map", "Ct"], "CSV_COLUMN=FIELD"),
    ],
)
def test_csv_plan_flags_are_rejected_before_a_write(invoke, fake, tmp_path, extra, match) -> None:
    args = ["changes", "plan", "--entry", ENTRY_ID, "--family", "results", *extra]
    if "--family" in extra:
        args = ["changes", "plan", "--entry", ENTRY_ID, *extra]
    code, payload, _ = invoke(*args)
    assert code == 2
    assert match in payload["error"]["message"]
    assert fake.calls == []


def test_schema_get_includes_dropdown_options(invoke) -> None:
    code, payload, _ = invoke("schema", "get", "entsch_choice")
    assert code == 0, payload
    status = payload["data"]["field_definitions"][0]
    assert status["dropdown"]["id"] == "sfs_status"
    assert [option["name"] for option in status["dropdown"]["options"]] == ["Open", "Closed"]
    assert status["dropdown"]["options"][1]["archived"] is True


def test_schema_find_returns_typed_field_definitions(invoke, fake) -> None:
    code, payload, _ = invoke("schema", "find", "Schema assaysch_abc123", "--kind", "result")
    assert code == 0, payload
    fields = {field["systemName"]: field for field in payload["data"]["field_definitions"]}
    assert fields["ct"]["type"] == "INTEGER"
    assert fields["ct"]["is_required"] is True
    assert fields["status"]["dropdown_options"][0]["id"] == "sfso_open"
    assert fake.writes() == []


def test_entry_commands_return_web_url(invoke) -> None:
    url = entry_web_url(ENTRY_ID)
    outputs = [
        invoke("entry", "resolve", "EXP-1"),
        invoke("entry", "get", ENTRY_ID),
        invoke("entry", "list", "--folder", "lib_abc123"),
        invoke("entry", "bulk-get", "--display-id", "EXP-1"),
    ]
    assert all(code == 0 for code, _, _ in outputs)
    resolved, entry, listed, bulk = (payload["data"] for _, payload, _ in outputs)
    assert resolved["web_url"] == entry["web_url"] == url
    assert listed["items"][0]["web_url"] == url
    assert bulk["items"][0]["web_url"] == url


def test_entry_read_help_mentions_web_url() -> None:
    for command in ("get", "list", "resolve", "bulk-get"):
        help_text = plain_stdout(["entry", command, "--help"])
        assert "web_url" in help_text, command


def test_cli_pages_like_the_mcp_tools(invoke, fake) -> None:
    fake.table_page_size = 2
    code, table, _ = invoke("table", "get", TABLE_PART, "--row-offset", "2", "--row-limit", "5")
    assert code == 0
    assert table["data"]["pagination"] == {
        "offset": 2,
        "returned": 1,
        "total": 3,
        "next_offset": None,
    }
    code, entry, _ = invoke(
        "entry", "get", ENTRY_ID, "--full", "--part-limit", "4", "--table-row-limit", "1"
    )
    assert code == 0
    assert entry["data"]["pagination"]["parts"]["next_offset"] == 4
    assert len(entry["data"]["tables"][TABLE_PART]["content"]["rows"]) == 1
    code, schema, _ = invoke("schema", "get", "entsch_abc123", "--field-limit", "5")
    assert code == 0
    assert schema["data"]["pagination"]["next_offset"] == 5


def test_cli_help_hides_mcp_only_text() -> None:
    text = plain_stdout(["changes", "apply", "--help"])
    assert "--confirm-env" in text
    assert "MCP:" not in text and "allow-writes list" not in text


def test_spec_json_plans_the_same_request_as_a_spec_file(invoke, fake, tmp_path) -> None:
    request = {"operation": "append_rows", "table_part_id": TABLE_PART, "rows": [["D", "4"]]}
    path = tmp_path / "spec.json"
    path.write_text(json.dumps(request), encoding="utf-8")
    base = ["changes", "plan", "--entry", ENTRY_ID, "--family", "table"]
    code, from_file, _ = invoke(*base, "--spec", str(path))
    assert code == 0, from_file
    code, inline, _ = invoke(*base, "--spec-json", json.dumps(request))
    assert code == 0, inline
    assert inline["data"]["request"] == from_file["data"]["request"] == request
    assert inline["data"]["after"] == from_file["data"]["after"]
    assert fake.writes() == []


def _usage_failure(args: list[str]) -> dict:
    result = CliRunner().invoke(cli.app, args)
    assert result.exit_code == ExitCode.USAGE, result.output
    payload = json.loads(result.stdout)
    assert payload["ok"] is False and payload["data"] is None
    assert payload["error"]["code"] == "usage_error"
    return payload["error"]


def test_usage_errors_have_their_own_exit_code() -> None:
    assert ExitCode.USAGE != ExitCode.VALIDATION
    assert len(set(ExitCode)) == len(list(ExitCode))


def test_unknown_option_is_a_usage_error_envelope(fake) -> None:
    error = _usage_failure(["entry", "get", ENTRY_ID, "--bogus"])
    assert error["message"] == "No such option: --bogus"
    assert error["details"]["command"].endswith("entry get")
    assert error["details"]["help"].endswith("entry get --help")
    assert error["details"]["usage"].startswith("Usage:")
    assert fake.calls == []


def test_unknown_command_is_a_usage_error_envelope() -> None:
    error = _usage_failure(["nope"])
    assert error["message"] == "No such command 'nope'."
    error = _usage_failure(["entry", "nope"])
    assert error["message"] == "No such command 'nope'."


def test_missing_argument_and_subcommand_are_usage_errors() -> None:
    assert _usage_failure(["entry", "get"])["message"] == "Missing argument 'reference'."
    assert _usage_failure(["entry"])["message"] == "Missing command."


def test_root_env_option_is_gone_and_the_error_says_where_it_goes() -> None:
    for flag in ("--env", "-e"):
        error = _usage_failure([flag, "sandbox", "entry", "get", ENTRY_ID])
        assert error["details"]["hint"].endswith("entry get REF --env NAME")


def test_bare_command_prints_help_and_help_still_succeeds() -> None:
    result = CliRunner().invoke(cli.app, [])
    assert result.exit_code == ExitCode.USAGE
    assert "Usage:" in result.stdout
    result = CliRunner().invoke(cli.app, ["--help"])
    assert result.exit_code == 0
    result = CliRunner().invoke(cli.app, ["entry", "get", "--help"])
    assert result.exit_code == 0


def test_completion_options_are_absent() -> None:
    help_text = plain_stdout(["--help"])
    assert "--install-completion" not in help_text
    assert "--show-completion" not in help_text
    _usage_failure(["--install-completion"])
    _usage_failure(["--show-completion"])


def test_entry_get_takes_env_after_the_reference(fake, monkeypatch) -> None:
    configure_env("dev", "https://your-tenant-dev.benchling.com")
    monkeypatch.setattr(cli, "Runtime", lambda environment: runtime_for(fake, environment))
    for args in (
        ["entry", "get", ENTRY_ID, "--env", "dev"],
        ["entry", "get", ENTRY_ID, "-e", "dev"],
        ["entry", "get", "--env", "dev", ENTRY_ID],
    ):
        result = CliRunner().invoke(cli.app, args)
        assert result.exit_code == 0, result.output
        payload = json.loads(result.stdout)
        assert payload["meta"]["env"] == "dev"
        assert payload["data"]["version_id"] == "ver_1"


def test_every_command_that_needs_an_env_takes_env() -> None:
    from benchlingtk.commands import TOOL_ATTR

    def walk(command, path=()):
        for name, child in getattr(command, "commands", {}).items():
            yield from walk(child, (*path, name))
        if not hasattr(command, "commands"):
            yield path, command

    root = typer.main.get_command(cli.app)
    assert [param.name for param in root.params] == ["version"]
    checked = 0
    for path, command in walk(root):
        declared = getattr(command.callback, TOOL_ATTR, None)
        if declared is not None and not declared.needs_env:
            continue
        if path[0] in ("mcp", "skill"):
            continue
        options = {opt for param in command.params for opt in param.opts}
        assert {"--env", "-e"} <= options, path
        checked += 1
    assert checked > 20


def test_auth_commands_take_env_after_the_subcommand(benchling_env) -> None:
    for args in (
        ["auth", "status", "--env", "sandbox"],
        ["auth", "protect", "--env", "sandbox"],
        ["auth", "unprotect", "-e", "sandbox"],
    ):
        result = CliRunner().invoke(cli.app, args)
        assert result.exit_code == 0, result.output
        assert json.loads(result.stdout)["meta"]["env"] == "sandbox"


def test_non_standalone_callers_still_get_the_click_exception() -> None:
    with pytest.raises(cli._CLICK_USAGE_ERROR):
        cli.app(["--bogus"], standalone_mode=False)


def test_other_click_failures_keep_their_text_and_exit_code(monkeypatch) -> None:
    def fail(*args) -> None:
        raise cli._CLICK_EXCEPTION("disk is full")

    monkeypatch.setattr(cli, "_execute", fail)
    result = CliRunner().invoke(cli.app, ["entry", "get", ENTRY_ID, "--env", "dev"])
    assert result.exit_code == 1
    assert "disk is full" in result.stderr

    def abort(*args) -> None:
        raise typer.Abort()

    monkeypatch.setattr(cli, "_execute", abort)
    result = CliRunner().invoke(cli.app, ["entry", "get", ENTRY_ID, "--env", "dev"])
    assert result.exit_code == 1
    assert "Aborted!" in result.stderr
