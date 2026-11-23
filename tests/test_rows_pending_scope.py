"""`results rows` reads table rows only; no doc or help text may claim it takes pending results.

Pending results are seen after a push with `results list --include-pending`,
`results pending --entry`, `results diff --include-pending` and
`results verify --payload`, so `results rows` deliberately has no pending option.
"""

from __future__ import annotations

import re
from pathlib import Path

import click
import pytest
import typer.main

from benchling_tools import cli
from benchling_tools.mcp_server import build_mcp_server
from benchling_tools.row_filter import WHERE_NOTE

ROOT = Path(__file__).resolve().parents[1]
# "results rows … --include-pending" (or the MCP form) inside one phrase or code span.
CLAIMS = (
    re.compile(r"results rows[^`.;\n]*--include-pending"),
    re.compile(r"results_rows[^`.;\n]*include_pending"),
)
# Help text avoids this command's own flags (such as --entry), which MCP renames.
POINTERS = (
    "results list --include-pending",
    "results pending (each given the entry)",
    "results diff --include-pending",
    "results verify --payload FILE",
)


def _rows_command() -> click.Command:
    group = typer.main.get_command(cli.app)
    return group.commands["results"].commands["rows"]  # type: ignore[attr-defined, no-any-return]


def _text_files() -> list[Path]:
    return [
        ROOT / "README.md",
        ROOT / "SKILL.md",
        ROOT / "CHANGELOG.md",
        *sorted((ROOT / "docs").glob("*.md")),
    ]


def _claims(text: str) -> list[str]:
    return [match.group(0) for pattern in CLAIMS for match in pattern.finditer(text)]


def test_results_rows_has_no_pending_option() -> None:
    options = {opt for param in _rows_command().params for opt in getattr(param, "opts", [])}
    assert "--include-pending" not in options
    assert "--pending-created-after" not in options


def test_results_rows_help_points_at_the_pending_commands() -> None:
    help_text = " ".join((_rows_command().help or "").split())
    for pointer in POINTERS:
        assert pointer in help_text
        assert pointer in WHERE_NOTE
    assert _claims(help_text) == []


@pytest.mark.parametrize("path", _text_files(), ids=lambda path: path.name)
def test_docs_do_not_claim_results_rows_takes_pending(path: Path) -> None:
    assert _claims(path.read_text(encoding="utf-8")) == []


async def test_mcp_results_rows_has_no_pending_argument(pool) -> None:
    tools = {tool.name: tool for tool in await build_mcp_server(pool).list_tools()}
    rows = tools["results_rows"]
    assert "include_pending" not in rows.inputSchema["properties"]
    assert "pending_created_after" not in rows.inputSchema["properties"]
    for pointer in POINTERS:
        assert pointer in " ".join((rows.description or "").split())
    for tool in tools.values():
        assert _claims(tool.description or "") == [], tool.name


def test_claim_pattern_catches_the_overstated_wording() -> None:
    assert _claims("Run `results rows TABLE --include-pending` after a push.")
    assert _claims("MCP results_rows takes include_pending")
    assert not _claims(
        "`results rows` has no pending option; use `results diff --include-pending`."
    )
