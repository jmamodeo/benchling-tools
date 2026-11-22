"""Issue drafts: template, Benchling docs citations, redaction, CLI, and MCP tool."""

from __future__ import annotations

import copy
import json
import subprocess
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest
import yaml
from mcp.shared.memory import create_connected_server_and_client_session as Client
from typer.testing import CliRunner

from benchling_tools import __version__, cli, issues
from benchling_tools.errors import ValidationError
from benchling_tools.issues import (
    DocsCitation,
    build_issue_draft,
    find_sensitive,
    is_benchling_docs_url,
    validate_citations,
)
from benchling_tools.mcp_server import build_mcp_server
from benchling_tools.redact import register_secret

ROOT = Path(__file__).resolve().parents[1]
DOCS_URL = "https://docs.benchling.com/docs/example-creating-results"
QUOTE = (
    "When results are created in this way, they are not immediately appended to the "
    "destination table."
)

SPEC: dict = {
    "kind": "feature",
    "summary": "Fill a results table without a manual accept",
    "attempted_action": "Asked the agent to upload assay results into a notebook table.",
    "expected": "The results appear in the table.",
    "actual": "The results were created as pending and next_step asked for a click.",
    "interface": "mcp",
    "command_or_tool": "changes_apply",
    "description": (
        "Results created through the API land as pending with no entry, so an agent "
        "cannot finish an upload end to end."
    ),
    "proposed_solution": "Add a plan family that creates the entry with initial table data.",
    "citations": [{"url": DOCS_URL, "quote": QUOTE}],
}


def spec(**changes):
    value = copy.deepcopy(SPEC)
    value.update(changes)
    return value


def test_feature_draft_renders_every_section_and_a_prefilled_url() -> None:
    draft = build_issue_draft(spec())
    assert draft["title"] == "Feature: Fill a results table without a manual accept"
    assert draft["labels"] == ["enhancement"]
    assert draft["kind"] == "feature"
    assert draft["filed"] is False
    body = draft["body"]
    for heading in (
        "## What the user tried",
        "## Expected behaviour",
        "## Actual behaviour",
        "## Environment",
        "## Description",
        "## Proposed solution",
        "## Benchling documentation",
    ):
        assert heading in body
    assert f"benchling-tools version: {__version__}" in body
    assert "Interface: MCP" in body
    assert "`changes_apply`" in body
    assert f"- {DOCS_URL}" in body
    assert f"> {QUOTE}" in body
    assert draft["environment"]["toolkit_version"] == __version__

    url = urlsplit(draft["new_issue_url"])
    assert f"{url.scheme}://{url.netloc}{url.path}" == issues.NEW_ISSUE_URL
    query = parse_qs(url.query)
    assert query["title"] == [draft["title"]]
    assert query["body"] == [body]
    assert query["labels"] == ["enhancement"]
    assert draft["body_in_url"] is True


def test_bug_draft_uses_the_bug_label() -> None:
    draft = build_issue_draft(
        spec(kind="bug", interface="cli", command_or_tool="benchling-tools table get")
    )
    assert draft["title"].startswith("Bug: ")
    assert draft["labels"] == ["bug"]
    assert "**Type:** Bug" in draft["body"]
    assert "Interface: CLI" in draft["body"]


def test_a_long_body_is_left_out_of_the_url() -> None:
    draft = build_issue_draft(spec(description="word " * 2000))
    assert draft["body_in_url"] is False
    query = parse_qs(urlsplit(draft["new_issue_url"]).query)
    assert "body" not in query
    assert query["labels"] == ["enhancement"]
    assert len(draft["new_issue_url"]) <= issues.MAX_URL_LENGTH


@pytest.mark.parametrize(
    "url",
    [
        "https://docs.benchling.com/docs/working-with-tables",
        "https://help.benchling.com/hc/en-us/articles/123-Permissions-overview",
        "https://benchling.com/api/v2/reference#/Assay%20Results/bulkCreateAssayResults",
        "https://www.benchling.com/api/reference",
        "https://benchling.com/api/v3/openapi.yaml",
    ],
)
def test_official_docs_urls_are_accepted(url) -> None:
    assert is_benchling_docs_url(url)
    validate_citations([DocsCitation(url=url, quote=QUOTE)])


@pytest.mark.parametrize(
    "url",
    [
        "http://docs.benchling.com/docs/working-with-tables",
        "https://acme.benchling.com/api/v2/entries",
        "https://benchling.com/blog/post",
        "https://docs.benchling.com.evil.example/docs",
        "https://user:pw@docs.benchling.com/docs",
        "https://docs.benchling.com:8443/docs",
        "https://stackoverflow.com/questions/1",
        "docs.benchling.com/docs/working-with-tables",
    ],
)
def test_other_urls_are_not_docs(url) -> None:
    assert not is_benchling_docs_url(url)


def test_a_citation_is_required() -> None:
    with pytest.raises(ValidationError, match="Benchling docs citation"):
        build_issue_draft(spec(citations=[]))
    missing = spec()
    del missing["citations"]
    with pytest.raises(ValidationError, match="Benchling docs citation"):
        build_issue_draft(missing)
    with pytest.raises(ValidationError, match="Benchling docs citation"):
        validate_citations([])


def test_a_citation_must_be_official_and_quoted() -> None:
    with pytest.raises(ValidationError) as caught:
        build_issue_draft(spec(citations=[{"url": "https://example.com/blog", "quote": "short"}]))
    reasons = [problem["reason"] for problem in caught.value.details["citations"]]
    assert any("docs.benchling.com" in reason for reason in reasons)
    assert any("at least" in reason for reason in reasons)
    with pytest.raises(ValidationError) as caught:
        build_issue_draft(spec(citations=[{"url": DOCS_URL, "quote": "x" * 1001}]))
    assert "at most" in caught.value.details["citations"][0]["reason"]


def test_missing_or_unknown_fields_are_validation_errors() -> None:
    with pytest.raises(ValidationError, match="fields are invalid") as caught:
        build_issue_draft(spec(kind="question", extra="x"))
    fields = {problem["field"] for problem in caught.value.details["problems"]}
    assert {"kind", "extra"} <= fields


@pytest.mark.parametrize(
    ("text", "kind"),
    [
        ("Seen on https://acme.benchling.com/acme/f/lib_x", "tenant_url"),
        ("Seen on acme-dev.benchling.com", "tenant_url"),
        ("entry etr_Ab3dEf9hIj failed", "entity_id"),
        ("table docpart_Zq7Lm2Np4R", "entity_id"),
        ("notebook EXP12345678", "display_id"),
        ("key sk_AbCdEfGhIjKlMnOpQrSt", "benchling_api_key"),
        ("token ghp_abcdefghijklmnopqrstuvwxyz0123", "github_token"),
        ("header eyJhbGciOiJIUzI1.eyJzdWIiOiIxMjM0NTY3.abcdefgh", "jwt"),
        ("Authorization: Bearer abcdefghijklmnop1234", "bearer_token"),
        ("AKIAABCDEFGHIJKLMNOP", "aws_access_key"),
        ("-----BEGIN RSA PRIVATE KEY-----", "private_key"),
        ("client_secret=hunter2hunter2", "credential_assignment"),
        ("run 3f2a9c1e7b6d4a5f8e0c2b1a9d8e7f6a", "long_random_token"),
        ("ask jane.doe@example.org", "email_address"),
    ],
)
def test_sensitive_text_is_refused(text, kind) -> None:
    findings = find_sensitive({"description": text})
    assert {"field": "description", "kind": kind} in findings
    with pytest.raises(ValidationError, match="tenant data") as caught:
        build_issue_draft(spec(actual=f"{SPEC['actual']} {text}"))
    assert {"field": "actual", "kind": kind} in caught.value.details["findings"]
    assert text not in json.dumps(caught.value.details)


def test_registered_secrets_are_refused() -> None:
    register_secret("a-registered-client-secret")
    assert find_sensitive({"expected": "uses a-registered-client-secret"}) == [
        {"field": "expected", "kind": "known_secret"}
    ]


def test_sensitive_values_in_citations_are_refused() -> None:
    with pytest.raises(ValidationError) as caught:
        build_issue_draft(spec(citations=[{"url": DOCS_URL, "quote": f"{QUOTE} etr_Ab3dEf9hIj"}]))
    assert {"field": "citations[0].quote", "kind": "entity_id"} in caught.value.details["findings"]


@pytest.mark.parametrize(
    "text",
    [
        "Placeholders such as https://your-tenant.benchling.com and your-tenant-dev.benchling.com",
        "Use etr_XXXXXX or docpart_REPLACE_ME, and EXP-0001",
        "The tools plan_changes, apply_changes, list_results_table_rows, and get_change_status",
        "See https://help.benchling.com/hc/en-us/articles/13810222262285-How-can-I-associate-results",
        "PATCH /api/v2/assay-results with documentPartResultsTableId",
    ],
)
def test_ordinary_text_and_placeholders_pass(text) -> None:
    assert find_sensitive({"description": text}) == []


@pytest.fixture
def runner():
    return CliRunner()


def _invoke(runner, *args, input=None):
    result = runner.invoke(cli.app, ["issue", "draft", *args], input=input)
    return result.exit_code, json.loads(result.stdout)


def test_cli_draft_from_a_spec_file_files_nothing(runner, tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(subprocess, "run", pytest.fail)
    path = tmp_path / "issue.json"
    path.write_text(json.dumps(SPEC), encoding="utf-8")
    code, payload = _invoke(runner, "--spec", str(path))
    assert code == 0
    assert payload["ok"] is True
    assert payload["data"]["filed"] is False
    assert payload["data"]["labels"] == ["enhancement"]
    assert payload["meta"] == {"repository": "jmamodeo/benchling-tools"}


def test_cli_draft_from_flags(runner) -> None:
    code, payload = _invoke(
        runner,
        "--kind", "bug",
        "--summary", SPEC["summary"],
        "--attempted", SPEC["attempted_action"],
        "--expected", SPEC["expected"],
        "--actual", SPEC["actual"],
        "--interface", "cli",
        "--command", "benchling-tools changes apply",
        "--description", SPEC["description"],
        "--solution", SPEC["proposed_solution"],
        "--cite-url", DOCS_URL,
        "--cite-quote", QUOTE,
    )  # fmt: skip
    assert code == 0, payload
    assert payload["data"]["labels"] == ["bug"]
    assert "`benchling-tools changes apply`" in payload["data"]["body"]


def test_cli_flags_override_the_spec_on_stdin(runner) -> None:
    code, payload = _invoke(runner, "--spec", "-", "--kind", "bug", input=json.dumps(SPEC))
    assert code == 0
    assert payload["data"]["kind"] == "bug"


def test_cli_cite_flags_must_pair(runner) -> None:
    code, payload = _invoke(runner, "--spec", "-", "--cite-url", DOCS_URL, input=json.dumps(SPEC))
    assert code == 2
    assert "--cite-quote" in payload["error"]["message"]


def test_cli_refuses_a_draft_with_tenant_data(runner) -> None:
    code, payload = _invoke(
        runner, "--spec", "-", "--actual", "failed on acme.benchling.com", input=json.dumps(SPEC)
    )
    assert code == 2
    assert payload["error"]["details"]["findings"] == [{"field": "actual", "kind": "tenant_url"}]


class _Completed:
    def __init__(self, returncode: int, stdout: str = "", stderr: str = "") -> None:
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def test_cli_file_calls_gh_only_with_the_flag(runner, monkeypatch) -> None:
    calls = []

    def fake_run(command, **kwargs):
        calls.append((command, kwargs))
        return _Completed(0, "https://github.com/jmamodeo/benchling-tools/issues/999\n")

    monkeypatch.setattr("shutil.which", lambda name: "/usr/bin/gh")
    monkeypatch.setattr(subprocess, "run", fake_run)
    code, payload = _invoke(runner, "--spec", "-", input=json.dumps(SPEC))
    assert code == 0 and calls == []

    code, payload = _invoke(runner, "--spec", "-", "--file", input=json.dumps(SPEC))
    assert code == 0, payload
    assert payload["data"]["filed"] is True
    assert payload["data"]["issue_url"].endswith("/issues/999")
    command, kwargs = calls[0]
    assert command[:5] == ["/usr/bin/gh", "issue", "create", "--repo", "jmamodeo/benchling-tools"]
    assert ["--label", "enhancement"] == command[
        command.index("--label") : command.index("--label") + 2
    ]
    assert command[-2:] == ["--body-file", "-"]
    assert kwargs["input"] == payload["data"]["body"]


def test_cli_file_reports_a_gh_failure(runner, monkeypatch) -> None:
    monkeypatch.setattr("shutil.which", lambda name: "/usr/bin/gh")
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: _Completed(1, stderr="not logged in"))
    code, payload = _invoke(runner, "--spec", "-", "--file", input=json.dumps(SPEC))
    assert code == 7
    assert "nothing was filed" in payload["error"]["message"]
    assert payload["error"]["details"]["reason"] == "not logged in"
    assert payload["error"]["details"]["new_issue_url"].startswith(issues.NEW_ISSUE_URL)


def test_cli_file_without_gh(runner, monkeypatch) -> None:
    monkeypatch.setattr("shutil.which", lambda name: None)
    code, payload = _invoke(runner, "--spec", "-", "--file", input=json.dumps(SPEC))
    assert code == 6
    assert payload["error"]["code"] == "unsupported_capability"


def mcp_arguments(draft: dict) -> dict:
    """The issue_draft tool takes the CLI's flags, so citations are cite_url/cite_quote lists."""
    arguments = dict(draft)
    arguments["attempted"] = arguments.pop("attempted_action")
    arguments["solution"] = arguments.pop("proposed_solution")
    citations = arguments.pop("citations")
    arguments["cite_url"] = [citation["url"] for citation in citations]
    arguments["cite_quote"] = [citation["quote"] for citation in citations]
    return arguments


async def test_mcp_issue_draft_is_read_only_and_needs_no_env(pool) -> None:
    server = build_mcp_server(pool)
    async with Client(server._mcp_server) as client:
        tools = {tool.name: tool for tool in (await client.list_tools()).tools}
        tool = tools["issue_draft"]
        assert tool.annotations.readOnlyHint is True
        assert "env" not in tool.inputSchema.get("properties", {})
        result = await client.call_tool("issue_draft", mcp_arguments(SPEC))
        payload = result.structuredContent
        assert payload["ok"] is True
        assert payload["data"]["filed"] is False
        assert "never files" in payload["data"]["next_step"]

        bad = await client.call_tool(
            "issue_draft",
            mcp_arguments(spec(citations=[{"url": "https://example.com/x", "quote": QUOTE}])),
        )
        assert bad.structuredContent["ok"] is False
        assert bad.structuredContent["error"]["code"] == "validation_error"


async def test_mcp_instructions_carry_the_issue_rule(pool) -> None:
    instructions = build_mcp_server(pool).instructions
    for phrase in ("ALWAYS explain the", "bug or a feature request", "explicit yes", "issue_draft"):
        assert phrase in instructions


def test_issue_forms_require_a_benchling_docs_citation() -> None:
    forms = ROOT / ".github" / "ISSUE_TEMPLATE"
    for name, label in (("bug_report.yml", "bug"), ("feature_request.yml", "enhancement")):
        form = yaml.safe_load((forms / name).read_text(encoding="utf-8"))
        assert form["labels"] == [label]
        citation = next(item for item in form["body"] if item.get("id") == "benchling_docs")
        assert citation["validations"]["required"] is True
        assert "docs.benchling.com" in citation["attributes"]["description"]
    assert not (forms / "bug_report.md").exists()
    assert not (forms / "feature_request.md").exists()
