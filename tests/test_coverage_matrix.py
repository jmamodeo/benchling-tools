"""The downstream app coverage matrix names real commands and gives every gap a follow-up."""

from __future__ import annotations

import re
from pathlib import Path

import typer

from benchling_tools.cli import app
from benchling_tools.mcp_server import cli_commands, tool_name

ROOT = Path(__file__).resolve().parents[1]
MATRIX = (ROOT / "docs" / "coverage-matrix.md").read_text(encoding="utf-8")
README = (ROOT / "README.md").read_text(encoding="utf-8")

_COMMANDS = {" ".join(path) for path, _ in cli_commands(typer.main.get_command(app))}
_TOOLS = {tool_name(tuple(command.split())) for command in _COMMANDS}
_GROUPS = {command.split()[0] for command in _COMMANDS}
_ALLOWED_HOSTS = {"github.com", "benchling.com", "docs.benchling.com", "help.benchling.com"}


def _rows() -> list[list[str]]:
    rows = []
    for line in MATRIX.splitlines():
        if (
            line.startswith("| ")
            and not line.startswith("| ---")
            and not line.startswith("| Workflow")
        ):
            rows.append([cell.strip() for cell in line.strip("|").split("|")])
    return rows


def test_readme_links_the_matrix() -> None:
    assert "(docs/coverage-matrix.md)" in README


def test_every_row_has_a_known_status() -> None:
    rows = _rows()
    assert len(rows) > 20
    for row in rows:
        assert len(row) == 4, row
        assert row[2] in {"covered", "partial", "gap", "out of scope"}, row


def test_every_partial_or_gap_has_a_follow_up() -> None:
    proposed = set(re.findall(r"^### (G\d+)\.", MATRIX, flags=re.MULTILINE))
    assert proposed
    for workflow, _, status, notes in _rows():
        if status not in {"partial", "gap"}:
            continue
        issues = re.findall(r"\(https://github\.com/jmamodeo/benchling-tools/issues/\d+\)", notes)
        drafts = set(re.findall(r"\bG\d+\b", notes))
        assert issues or drafts, workflow
        assert drafts <= proposed, workflow


def test_named_commands_and_tools_exist() -> None:
    for row in _rows():
        for code in re.findall(r"`([^`]+)`", row[1]):
            words = code.split()
            if words[0] in _GROUPS:
                command = " ".join(word for word in words if re.fullmatch(r"[a-z][a-z-]*", word))
                assert any(
                    command == known or command.startswith(known + " ") for known in _COMMANDS
                ), code
            elif re.fullmatch(r"[a-z]+(_[a-z]+)+|doctor|export", code):
                assert code in _TOOLS, code


def test_matrix_has_no_tenant_data() -> None:
    for url in re.findall(r"https://[^\s)`]+", MATRIX):
        assert url.split("//", 1)[1].split("/", 1)[0] in _ALLOWED_HOSTS, url
    assert not re.search(r"\b[0-9a-f]{32}\b", MATRIX)
    assert not re.search(
        r"\b(?:etr|ent|seq|con|strtbl|docpart|result|assaysch)_[A-Za-z0-9]{4,}", MATRIX
    )
