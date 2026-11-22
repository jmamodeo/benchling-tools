"""The packaged SKILL.md: locating it, the `benchlingtk skill` commands, the MCP resource,
and the built wheel."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest
from mcp import Client
from typer.testing import CliRunner

from benchlingtk import __version__, cli, skill
from benchlingtk.mcp_server import SKILL_RESOURCE_URI, build_mcp_server

ROOT = Path(__file__).resolve().parents[1]
SKILL_BYTES = (ROOT / "SKILL.md").read_bytes()
WHEEL_MEMBER = f"benchlingtk-{__version__}.data/data/share/benchlingtk/SKILL.md"


def test_installed_data_file_is_preferred(tmp_path, monkeypatch) -> None:
    installed = tmp_path / "share" / "benchlingtk" / "SKILL.md"
    installed.parent.mkdir(parents=True)
    installed.write_text("installed\n", encoding="utf-8")
    monkeypatch.setattr(skill, "_data_roots", lambda: [tmp_path])
    assert skill.find_skill_md() == installed
    assert skill.read_skill_md() == "installed\n"


def test_source_checkout_is_the_fallback(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(skill, "_data_roots", lambda: [tmp_path])
    assert skill.find_skill_md() == ROOT / "SKILL.md"


def test_checkout_without_pyproject_is_not_used(tmp_path, monkeypatch) -> None:
    (tmp_path / "SKILL.md").write_text("stray\n", encoding="utf-8")
    monkeypatch.setattr(skill, "_data_roots", lambda: [tmp_path / "prefix"])
    monkeypatch.setattr(skill, "_REPO_ROOT", tmp_path)
    with pytest.raises(skill.NotFoundError) as caught:
        skill.find_skill_md()
    assert str(tmp_path / "SKILL.md") in caught.value.details["searched"]


def test_data_roots_include_prefixes_and_user_scheme() -> None:
    roots = skill._data_roots()
    assert Path(sys.prefix) in roots
    assert Path(sys.base_prefix) in roots
    assert len(roots) == len(set(roots))


def test_data_roots_skip_schemes_without_a_data_path(monkeypatch) -> None:
    monkeypatch.setattr(skill.sysconfig, "get_paths", lambda scheme=None: {})
    assert skill._data_roots() == list(dict.fromkeys([Path(sys.prefix), Path(sys.base_prefix)]))


def test_skill_path_and_show_print_the_file() -> None:
    runner = CliRunner()
    path = runner.invoke(cli.app, ["skill", "path"])
    assert path.exit_code == 0, path.output
    assert Path(path.output.strip()).read_bytes() == SKILL_BYTES
    show = runner.invoke(cli.app, ["skill", "show"])
    assert show.exit_code == 0, show.output
    assert show.output.encode("utf-8") == SKILL_BYTES


def test_skill_commands_report_a_missing_file(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(skill, "_data_roots", lambda: [tmp_path])
    monkeypatch.setattr(skill, "_REPO_ROOT", tmp_path)
    for command in ("path", "show"):
        result = CliRunner().invoke(cli.app, ["skill", command])
        assert result.exit_code == int(skill.NotFoundError.exit_code)
        payload = json.loads(result.output)
        assert payload["ok"] is False
        assert payload["error"]["code"] == "not_found"


async def test_mcp_serves_skill_md_as_a_resource(pool) -> None:
    async with Client(build_mcp_server(pool)) as client:
        listed = await client.list_resources()
        assert [str(item.uri) for item in listed.resources] == [SKILL_RESOURCE_URI]
        assert listed.resources[0].mime_type == "text/markdown"
        result = await client.read_resource(SKILL_RESOURCE_URI)
    assert result.contents[0].text.encode("utf-8") == SKILL_BYTES


def _wheel(tmp_path: Path) -> Path:
    """The wheel in BENCHLINGTK_WHEEL_DIR (CI build job), else one built here."""
    configured = os.environ.get("BENCHLINGTK_WHEEL_DIR")
    if configured:
        out = Path(configured)
    else:
        uv = shutil.which("uv")
        if uv is None:
            pytest.skip("uv is needed to build the wheel")
        out = tmp_path / "dist"
        subprocess.run(
            [uv, "build", "--wheel", "--out-dir", str(out), str(ROOT)],
            check=True,
            capture_output=True,
        )
    wheels = sorted(out.glob(f"benchlingtk-{__version__}-*.whl"))
    assert len(wheels) == 1, wheels
    return wheels[0]


@pytest.mark.wheel
def test_built_wheel_ships_identical_skill_md(tmp_path) -> None:
    with zipfile.ZipFile(_wheel(tmp_path)) as wheel:
        assert WHEEL_MEMBER in wheel.namelist()
        assert wheel.read(WHEEL_MEMBER) == SKILL_BYTES


@pytest.mark.wheel
def test_built_wheel_does_not_advertise_a_typed_python_api(tmp_path) -> None:
    with zipfile.ZipFile(_wheel(tmp_path)) as wheel:
        names = wheel.namelist()
        assert not [name for name in names if name.endswith("py.typed")]
        metadata = next(name for name in names if name.endswith(".dist-info/METADATA"))
        assert "Typing :: Typed" not in wheel.read(metadata).decode()
