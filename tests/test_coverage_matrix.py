"""The downstream coverage matrix names real commands and gives every gap a disposition."""

from __future__ import annotations

import re
from pathlib import Path

import typer

from benchling_tools.cli import app
from benchling_tools.mcp_server import cli_commands

ROOT = Path(__file__).resolve().parents[1]
MATRIX = (ROOT / "docs" / "coverage-matrix.md").read_text(encoding="utf-8")
STATUSES = {"covered", "partial", "gap"}
_ALLOWED_HOSTS = {"benchling.com", "docs.benchling.com", "help.benchling.com"}


def _rows(prefix: str) -> list[list[str]]:
    rows = []
    for line in MATRIX.splitlines():
        cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
        if line.startswith("|") and re.fullmatch(rf"{prefix}\d+", cells[0]):
            rows.append(cells)
    return rows


def _commands() -> set[str]:
    return {" ".join(path) for path, _ in cli_commands(typer.main.get_command(app))}


def test_workflow_rows_have_a_status_and_real_commands() -> None:
    rows = _rows("W")
    assert rows
    ids = [row[0] for row in rows]
    assert len(ids) == len(set(ids))
    commands = _commands()
    for row in rows:
        assert len(row) == 5, row
        _, _, command_cell, status, _ = row
        assert status in STATUSES, row
        for command in re.findall(r"`([^`]+)`", command_cell):
            assert command in commands, f"{row[0]} names unknown command {command!r}"
        if status == "gap":
            assert command_cell == "none", row


def test_every_partial_or_gap_has_a_follow_up_or_scope_decision() -> None:
    follow_ups = {row[0]: row for row in _rows("G")}
    assert follow_ups
    referenced: set[str] = set()
    for row in _rows("W"):
        if row[3] == "covered":
            continue
        notes = row[4]
        named = set(re.findall(r"\bG\d+\b", notes))
        assert named or "out of scope" in notes, row
        assert named <= follow_ups.keys(), row
        referenced |= named
        for follow_up in named:
            assert row[0] in follow_ups[follow_up][1], (row[0], follow_up)
    assert referenced == follow_ups.keys()


def test_follow_ups_cite_the_benchling_api_reference_with_a_quote() -> None:
    for row in _rows("G"):
        citation = row[3]
        assert re.search(r"https://(docs\.|help\.)?benchling\.com/", citation), row
        assert re.search(r"\"[^\"]+\"", citation), row
        assert row[4], row


def test_pain_points_have_a_disposition() -> None:
    rows = _rows("P")
    assert rows
    for row in rows:
        assert len(row) == 4 and row[3], row


def test_matrix_holds_no_tenant_urls() -> None:
    hosts = {
        url.split("//", 1)[1].split("/", 1)[0] for url in re.findall(r"https://[^\s)`]+", MATRIX)
    }
    assert hosts <= _ALLOWED_HOSTS
    assert not re.search(
        r"\b(etr|con|bfi|seq|plt|req|result|assaysch|docpart)_[A-Za-z0-9]+", MATRIX
    )
