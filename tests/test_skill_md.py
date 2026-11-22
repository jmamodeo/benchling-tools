"""SKILL.md stays in the Agent Skills shape and only documents real CLI commands."""

from __future__ import annotations

import re
import shlex
import tomllib
from pathlib import Path

import yaml
from typer.testing import CliRunner

from benchlingtk import __version__, cli

ROOT = Path(__file__).resolve().parents[1]
SKILL = (ROOT / "SKILL.md").read_text(encoding="utf-8")
LISTING = ROOT / "agensi" / "listing.yaml"
_URL = re.compile(r"https://[^\s)`]+")
_ALLOWED_HOSTS = {"github.com", "docs.benchling.com"}
_ALLOWED_FRONTMATTER = {
    "name",
    "description",
    "license",
    "compatibility",
    "metadata",
    "allowed-tools",
}
# Lowercase letters, digits, and hyphens. No leading, trailing, or doubled hyphen.
_SKILL_NAME = re.compile(r"^[a-z0-9](?:[a-z0-9-]*[a-z0-9])?$")


def _frontmatter() -> dict[str, object]:
    assert SKILL.startswith("---\n")
    end = SKILL.index("\n---\n", 4)
    loaded = yaml.safe_load(SKILL[4:end])
    assert isinstance(loaded, dict)
    return loaded


def _console_commands() -> list[str]:
    commands: list[str] = []
    in_console = False
    pending = ""
    for line in SKILL.splitlines():
        if line.startswith("```"):
            if in_console and pending:
                commands.append(pending.strip())
                pending = ""
            in_console = not in_console and line[3:].strip() == "console"
            continue
        if not in_console:
            continue
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if stripped.endswith("\\"):
            pending = f"{pending} {stripped[:-1].strip()}".strip()
            continue
        commands.append(f"{pending} {stripped}".strip() if pending else stripped)
        pending = ""
    return commands


def test_frontmatter_follows_the_agent_skills_spec() -> None:
    meta = _frontmatter()
    assert set(meta) <= _ALLOWED_FRONTMATTER
    assert {"name", "description"} <= set(meta)

    name = meta["name"]
    assert isinstance(name, str)
    assert _SKILL_NAME.fullmatch(name)
    assert "--" not in name
    assert 1 <= len(name) <= 64
    assert name == "benchlingtk"
    assert name == ROOT.name

    description = meta["description"]
    assert isinstance(description, str)
    assert description.strip()
    assert len(description) <= 1024
    assert "Use when" in description
    assert "benchlingtk auth login --env NAME" in description
    assert "--allow-writes ENV" in description

    assert meta["license"] == "Apache-2.0"
    compatibility = meta["compatibility"]
    assert isinstance(compatibility, str)
    assert len(compatibility) <= 500

    metadata = meta["metadata"]
    assert isinstance(metadata, dict)
    assert all(isinstance(key, str) and isinstance(value, str) for key, value in metadata.items())
    summary = metadata["summary"]
    assert summary.strip()
    assert len(summary) <= 300
    assert metadata["version"] == __version__
    assert metadata["docs_url"] == "https://github.com/jmamodeo/benchlingtk"


def test_listing_lists_stay_out_of_the_skill_frontmatter() -> None:
    loaded = yaml.safe_load(LISTING.read_text(encoding="utf-8"))
    assert isinstance(loaded, dict)
    assert set(loaded) == {"tags", "keywords", "use_cases"}
    tags = loaded["tags"]
    keywords = loaded["keywords"]
    use_cases = loaded["use_cases"]
    assert isinstance(tags, list) and 1 <= len(tags) <= 10
    assert isinstance(keywords, list) and 1 <= len(keywords) <= 10
    assert isinstance(use_cases, list) and 1 <= len(use_cases) <= 8
    for items in (tags, keywords, use_cases):
        assert all(isinstance(item, str) and item.strip() for item in items)


def test_body_covers_the_read_and_preview_workflows() -> None:
    body = SKILL.split("\n---\n", 1)[1]
    for phrase in (
        "read-only by default",
        "--allow-writes ENV",
        "OS keychain",
        "benchlingtk auth login --env NAME",
        "entry list",
        "entry inspect",
        "table get",
        "results list",
        "results rows",
        "results tables",
        "export --kind",
        "changes plan",
        "changes apply",
        "issue draft",
        "When the toolkit cannot do it",
        "ALWAYS",
        "explicit yes",
        "docs.benchling.com",
    ):
        assert phrase in body
    assert "client secret" in body.casefold()
    assert "Run `auth login` only when the person has given you the credentials" in body
    assert "never invent a tenant URL, client ID, or secret" in body


def test_skill_pairs_the_toolkit_with_a_browser_without_driving_one() -> None:
    body = SKILL.split("\n---\n", 1)[1]
    description = _frontmatter()["description"]
    assert isinstance(description, str)
    assert "The toolkit does not drive a browser" in description
    assert "browser automation" not in description
    assert "never drives a browser" in body
    assert "Do not automate the browser" not in body
    assert "the task needs the Benchling website rather than the API" not in body
    sections = re.findall(r"^## (.+)$", body, flags=re.MULTILINE)
    pending = sections.index("Results land as pending")
    assert sections[pending + 1] == "Working alongside the browser"
    alongside = body.split("## Working alongside the browser", 1)[1].split("\n## ", 1)[0]
    for phrase in ("`web_url`", "results pending --plan PLAN_ID", "results verify PLAN_ID"):
        assert phrase in alongside
    assert (
        "The toolkit writes as the app; the browser acts as the signed-in user, so the "
        "signed-in user needs access to the entry and the app needs access to its folder."
    ) in alongside


def test_skill_urls_are_the_repository_and_benchling_docs_only() -> None:
    hosts = set()
    for url in _URL.findall(SKILL):
        hosts.add(url.split("//", 1)[1].split("/", 1)[0])
    assert hosts == _ALLOWED_HOSTS


def test_every_documented_command_is_accepted_by_the_cli() -> None:
    commands = _console_commands()
    assert len(commands) >= 15
    runner = CliRunner()
    for command in commands:
        argv = shlex.split(command)
        assert argv[0] == "benchlingtk", command
        if "--help" not in argv:
            argv.append("--help")
        result = runner.invoke(cli.app, argv[1:])
        assert result.exit_code == 0, (command, result.output)
        assert "Usage:" in result.output


def test_pip_package_ships_skill_md() -> None:
    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    data_files = pyproject["tool"]["setuptools"]["data-files"]
    assert "SKILL.md" in data_files["share/benchlingtk"]
