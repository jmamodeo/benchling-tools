"""Interview-readiness docs stay present, fixture-only, and tied to the demo cast."""

from __future__ import annotations

import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
README = (ROOT / "README.md").read_text(encoding="utf-8")
DEMO = (ROOT / "docs" / "demo.md").read_text(encoding="utf-8")
ARCHITECTURE = (ROOT / "ARCHITECTURE.md").read_text(encoding="utf-8")
THREAT = (ROOT / "docs" / "threat-model.md").read_text(encoding="utf-8")
CAST = (ROOT / "docs" / "demo.cast").read_text(encoding="utf-8")

_PLAN_ID = re.compile(r"\b[0-9a-f]{32}\b")
_URL = re.compile(r"https://[^\s)`]+")
_ALLOWED_HOSTS = {
    "your-tenant.benchling.com",
    "your-tenant-dev.benchling.com",
    "www.benchling.com",
    "docs.benchling.com",
    "help.benchling.com",
    "github.com",
    "img.shields.io",
    "modelcontextprotocol.io",
    "keepachangelog.com",
    "semver.org",
    "docs.astral.sh",
}


def _hosts(text: str) -> set[str]:
    hosts: set[str] = set()
    for url in _URL.findall(text):
        host = url.split("//", 1)[1].split("/", 1)[0].strip("`.,;:\"'")
        if host:
            hosts.add(host)
    return hosts


def test_architecture_explains_plan_apply_and_journal() -> None:
    assert "```mermaid" in ARCHITECTURE
    assert "ChangePlanner.plan" in ARCHITECTURE
    assert "ChangePlanner.apply" in ARCHITECTURE
    assert "applying" in ARCHITECTURE
    assert "checksum_for" in ARCHITECTURE
    assert "modifiedAt" in ARCHITECTURE


def test_threat_model_covers_the_four_risks() -> None:
    lowered = THREAT.casefold()
    for phrase in (
        "client secret",
        "stale",
        "replay",
        "write",
        "agent",
        "--allow-writes",
        "checksum",
        "idempotency",
    ):
        assert phrase in lowered
    assert "ChangePlanner._check_plan_scope" in THREAT
    assert "BenchlingAPI.request" in THREAT
    assert "plans/result_update.py" in THREAT


def test_readme_embeds_the_fixture_demo() -> None:
    assert "![Scripted playback of the offline fixture demo:" in README
    assert "(docs/demo.gif)" in README
    assert "(docs/demo.md)" in README
    assert "![Scripted playback of the offline fixture demo:" in DEMO
    assert "(demo.gif)" in DEMO
    assert "(demo.cast)" in DEMO
    assert "not a capture from the asciinema recorder" in DEMO
    start = DEMO.index("<!-- demo-transcript:start -->")
    end = DEMO.index("<!-- demo-transcript:end -->")
    transcript = DEMO[start:end]
    assert "remote writes so far: 0" in transcript
    assert "MCP writes are disabled for demo" in transcript
    cast_ids = set(_PLAN_ID.findall(CAST))
    transcript_ids = set(_PLAN_ID.findall(transcript))
    assert cast_ids
    assert cast_ids == transcript_ids


def test_cast_is_asciinema_v2_from_the_fixture() -> None:
    header, *events = CAST.splitlines()
    parsed = json.loads(header)
    assert parsed["version"] == 2
    assert parsed["title"] == "benchlingtk fixture demo"
    assert "USER" not in parsed.get("env", {})
    assert events
    payload = "\n".join(json.loads(line)[2] for line in events)
    assert "remote writes in total: 1" in payload
    assert "Entry version changed after the plan was created" in payload
    assert "Plan has already been applied or attempted" in payload
    assert "client_secret" not in payload
    assert "access_token" not in payload
    gif = ROOT / "docs" / "demo.gif"
    assert gif.read_bytes()[:6] == b"GIF89a"


def test_new_docs_use_placeholder_hosts_only() -> None:
    demo = README.split("## Demo", 1)[1].split("## Install", 1)[0]
    combined = "\n".join([ARCHITECTURE, THREAT, CAST, demo, DEMO])
    unexpected = _hosts(combined) - _ALLOWED_HOSTS
    assert unexpected == set()
