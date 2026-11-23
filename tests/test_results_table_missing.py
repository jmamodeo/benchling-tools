"""A results plan needs the results table in the entry first.

Benchling writes results only into a results table that already exists in the
entry ("First add a results table with the proper result schema to a Notebook
entry", docs.benchling.com, Uploading Results to Benchling through the API). A
results plan whose records name a schema with no table on the entry fails at plan
time with results_table_missing, and its next_step names the document plan that
creates the table. The docs and help state the order up front.
"""

from __future__ import annotations

import json
import shlex
from pathlib import Path

import click
import pytest
import typer.main
from conftest import runtime_for
from fake_benchling import ENTRY_ID
from typer.testing import CliRunner

from benchling_tools import cli
from benchling_tools.errors import (
    ExitCode,
    NotFoundError,
    ResultsTableMissingError,
    ValidationError,
)
from benchling_tools.mcp_server import INSTRUCTIONS, build_mcp_server

ROOT = Path(__file__).resolve().parents[1]
TABLE = "strtbl_example"
ORDER = "The results table must exist in the entry before results can be written to it"
UPLOADING_RESULTS = "https://docs.benchling.com/docs/example-creating-results"
NEW_ENTRY_WITH_RESULTS = (
    "https://docs.benchling.com/docs/creating-a-new-notebook-entry-containing-results"
)


def record(schema_id: str = "assaysch_abc123") -> dict:
    return {"schemaId": schema_id, "fields": {"ct": {"value": 12}}}


def add_schema(fake, schema_id: str = "assaysch_used") -> None:
    fake.field_defs[schema_id] = fake.field_defs["assaysch_abc123"]


def add_table(fake, table_id: str, schema_id: str, name: str = "Other results") -> None:
    fake.extra_result_tables.append(
        {
            "type": "results_table",
            "apiId": table_id,
            "name": name,
            "assayResultSchemaId": schema_id,
            "columns": ["ct"],
        }
    )


def saved_plans(runtime) -> list[Path]:
    return list(runtime.repository.config.plans_dir.glob("*.json"))


async def plan(runtime, table_id: str, records: list[dict]):
    return await runtime.planner.plan(
        ENTRY_ID, "results", {"table_id": table_id, "assay_results": records}
    )


async def test_table_not_on_entry_and_no_table_for_the_schema(runtime, fake) -> None:
    add_schema(fake)
    with pytest.raises(ResultsTableMissingError) as caught:
        await plan(runtime, "strtbl_elsewhere", [record("assaysch_used")])
    error = caught.value
    assert error.code == "results_table_missing"
    assert error.exit_code == ExitCode.ACCESS_OR_NOT_FOUND
    assert isinstance(error, NotFoundError)
    assert error.message == (
        f'Entry {ENTRY_ID} has no results table for result schema "Schema assaysch_used" '
        f"(assaysch_used). {ORDER}."
    )
    details = error.details
    assert details["reason"] == "results_table_missing"
    assert details["entry_id"] == ENTRY_ID
    assert details["table_id"] == "strtbl_elsewhere"
    assert details["schema"] == {"id": "assaysch_used", "name": "Schema assaysch_used"}
    assert details["entry_tables"] == [
        {"table_id": TABLE, "name": "Assay results", "schema_id": "assaysch_abc123"}
    ]
    assert details["document_spec"] == {
        "operation": "insert",
        "position": "end_of_document",
        "parts": [
            {
                "contentType": "results_table",
                "name": "Schema assaysch_used",
                "resultSchemaId": "assaysch_used",
            }
        ],
    }
    argv = shlex.split(details["command"])
    assert argv[:7] == [
        "benchling-tools",
        "changes",
        "plan",
        "--family",
        "document",
        "--entry",
        ENTRY_ID,
    ]
    assert argv[7] == "--spec-json"
    assert json.loads(argv[8]) == details["document_spec"]
    assert argv[9:] == ["--env", "sandbox"]
    next_step = details["next_step"]
    assert next_step.startswith("Create the results table first with a document plan: ")
    assert details["command"] in next_step
    assert "separate write that needs its own approval" in next_step
    assert f"results tables {ENTRY_ID}" in next_step
    assert fake.writes() == []
    assert saved_plans(runtime) == []


async def test_table_on_entry_with_records_of_a_schema_the_entry_lacks(runtime, fake) -> None:
    add_schema(fake)
    with pytest.raises(ResultsTableMissingError) as caught:
        await plan(runtime, TABLE, [record(), record("assaysch_used")])
    assert caught.value.details["schema"]["id"] == "assaysch_used"
    assert caught.value.details["table_id"] == TABLE
    assert fake.writes() == []
    assert saved_plans(runtime) == []


async def test_unreadable_schema_name_falls_back_to_the_id(runtime, fake) -> None:
    with pytest.raises(ResultsTableMissingError) as caught:
        await plan(runtime, "strtbl_elsewhere", [record("assaysch_unknown")])
    details = caught.value.details
    assert details["schema"] == {"id": "assaysch_unknown", "name": None}
    assert "result schema assaysch_unknown." in caught.value.message
    assert details["document_spec"]["parts"][0]["name"] == "Results"


async def test_display_id_is_used_in_the_command_when_the_entry_has_one(runtime, fake) -> None:
    add_schema(fake)
    original = fake._get_entry_v2

    def with_display_id(call, entry_id):
        status, payload = original(call, entry_id)
        payload["entry"]["displayId"] = "EXP-1"
        return status, payload

    fake._get_entry_v2 = with_display_id
    try:
        with pytest.raises(ResultsTableMissingError) as caught:
            await plan(runtime, "strtbl_elsewhere", [record("assaysch_used")])
    finally:
        fake._get_entry_v2 = original
    details = caught.value.details
    assert details["entry_display_id"] == "EXP-1"
    assert shlex.split(details["command"])[6] == "EXP-1"


async def test_a_missing_table_in_one_batch_refuses_the_whole_plan(runtime, fake) -> None:
    add_schema(fake)
    with pytest.raises(ResultsTableMissingError) as caught:
        await runtime.planner.plan(
            ENTRY_ID,
            "results",
            {
                "batches": [
                    {"table_id": TABLE, "assay_results": [record()]},
                    {"table_id": "strtbl_elsewhere", "assay_results": [record("assaysch_used")]},
                ]
            },
        )
    assert caught.value.details["table_id"] == "strtbl_elsewhere"
    assert fake.writes() == []
    assert saved_plans(runtime) == []


async def test_wrong_table_id_when_the_entry_has_a_table_for_the_schema(runtime, fake) -> None:
    add_schema(fake)
    add_table(fake, "strtbl_used", "assaysch_used")
    with pytest.raises(NotFoundError) as caught:
        await plan(runtime, "strtbl_elsewhere", [record("assaysch_used")])
    error = caught.value
    assert not isinstance(error, ResultsTableMissingError)
    assert error.code == "not_found"
    assert error.details["reason"] == "result_table_not_on_entry"
    assert "strtbl_used" in error.details["next_step"]
    assert {item["table_id"] for item in error.details["entry_tables"]} == {TABLE, "strtbl_used"}
    assert fake.writes() == []


async def test_record_schema_on_another_table_stays_a_row_problem(runtime, fake) -> None:
    add_schema(fake, "assaysch_other")
    add_table(fake, "strtbl_other", "assaysch_other")
    with pytest.raises(ValidationError, match="does not match the selected table"):
        await plan(runtime, TABLE, [record("assaysch_other")])
    assert fake.writes() == []


async def test_a_matching_plan_reads_the_entry_no_extra_time(runtime, fake, monkeypatch) -> None:
    def entry_reads() -> int:
        return sum(
            1 for call in fake.calls if call.method == "GET" and call.path.endswith(ENTRY_ID)
        )

    await plan(runtime, TABLE, [record()])
    with_check = entry_reads()
    assert with_check > 0
    fake.calls.clear()

    async def skip(*_args, **_kwargs) -> None:
        return None

    monkeypatch.setattr(type(runtime.planner), "_check_results_table_exists", skip)
    await plan(runtime, TABLE, [record()])
    assert entry_reads() == with_check


async def test_the_suggested_document_plan_is_accepted_without_writes(runtime, fake) -> None:
    add_schema(fake)
    with pytest.raises(ResultsTableMissingError) as caught:
        await plan(runtime, "strtbl_elsewhere", [record("assaysch_used")])
    planned = await runtime.planner.plan(
        ENTRY_ID, "document", caught.value.details["document_spec"]
    )
    assert planned.family == "document"
    assert fake.writes() == []
    argv = shlex.split(caught.value.details["command"])[1:] + ["--help"]
    result = CliRunner().invoke(cli.app, argv)
    assert result.exit_code == 0, result.output


def test_cli_envelope_reports_results_table_missing(benchling_env, fake, monkeypatch) -> None:
    add_schema(fake)
    monkeypatch.setattr(cli, "Runtime", lambda environment: runtime_for(fake, environment))
    spec = {"table_id": "strtbl_elsewhere", "assay_results": [record("assaysch_used")]}
    result = CliRunner().invoke(
        cli.app,
        [
            "changes",
            "plan",
            "--family",
            "results",
            "--entry",
            ENTRY_ID,
            "--spec-json",
            json.dumps(spec),
            "--env",
            "sandbox",
        ],
    )
    assert result.exit_code == ExitCode.ACCESS_OR_NOT_FOUND
    payload = json.loads(result.stdout)
    assert payload["ok"] is False
    assert payload["error"]["code"] == "results_table_missing"
    assert payload["error"]["details"]["schema"]["id"] == "assaysch_used"
    assert "document plan" in payload["error"]["details"]["next_step"]
    assert fake.writes() == []


# ---- the order is stated up front ---------------------------------------


def _command(*names: str) -> click.Command:
    group: click.Command = typer.main.get_command(cli.app)
    for name in names:
        group = group.commands[name]  # type: ignore[attr-defined]
    return group


def _flat(text: str | None) -> str:
    return " ".join((text or "").split())


@pytest.mark.parametrize("name", ["SKILL.md", "docs/reference.md"])
def test_docs_state_the_table_first_order(name: str) -> None:
    text = _flat((ROOT / name).read_text(encoding="utf-8"))
    assert ORDER in text
    assert "results_table_missing" in text
    assert "results_table" in text and "resultSchemaId" in text
    assert UPLOADING_RESULTS in text
    assert NEW_ENTRY_WITH_RESULTS in text
    assert "separate write" in text
    check = text.index("Check with `results tables ENTRY`")
    create = text.index("document plan", check)
    then = (
        text.index("then plan the results", create)
        if name != "SKILL.md"
        else text.index("then plan the results with it", create)
    )
    assert check < create < then


def test_skill_places_the_order_before_the_result_upload_spec() -> None:
    text = (ROOT / "SKILL.md").read_text(encoding="utf-8")
    assert text.index("### Results need a table first") < text.index("Result upload. ")
    assert "Result upload. The results table must exist in the entry first" in text


def test_changelog_records_the_plan_time_error() -> None:
    text = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    section = text.split("## [0.8.0]", 1)[1].split("\n## [", 1)[0]
    assert "results_table_missing" in section
    assert UPLOADING_RESULTS in section


def test_cli_help_states_the_order() -> None:
    plan_help = _flat(_command("changes", "plan").help)
    assert ORDER in plan_help
    assert "results_table_missing" in plan_help
    assert plan_help.index(ORDER) < plan_help.index("Inline result uploads")
    tables_help = _flat(_command("results", "tables").help)
    assert ORDER in tables_help
    assert "document plan" in tables_help


async def test_mcp_descriptions_state_the_order(pool) -> None:
    tools = {tool.name: tool for tool in await build_mcp_server(pool).list_tools()}
    plan_tool = tools["changes_plan"]
    assert ORDER in _flat(plan_tool.description)
    spec = _flat(json.dumps(plan_tool.inputSchema["properties"]["spec"]))
    assert "already exists in the entry" in spec
    assert ORDER in _flat(tools["results_tables"].description)
    assert ORDER in _flat(INSTRUCTIONS)
