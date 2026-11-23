from __future__ import annotations

import os
import stat
import types
from pathlib import Path

import pytest

from benchling_tools import config
from benchling_tools.config import ConfigStore, application_home


@pytest.fixture
def clean_home(monkeypatch, tmp_path):
    monkeypatch.delenv("BENCHLING_TOOLS_HOME", raising=False)
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    monkeypatch.delenv("APPDATA", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path / "home"))
    return tmp_path / "home"


def test_override_is_expanded_and_resolved(monkeypatch, tmp_path, clean_home) -> None:
    monkeypatch.setenv("BENCHLING_TOOLS_HOME", "~/custom/../state")
    assert application_home() == (clean_home / "state").resolve()


def test_legacy_override_is_not_used(monkeypatch, clean_home) -> None:
    monkeypatch.setenv("BENCHLINGTK_HOME", str(clean_home / "old"))
    monkeypatch.setattr(config.sys, "platform", "linux")
    assert application_home() == clean_home / ".config" / "benchling-tools"


def test_linux_prefers_xdg_config_home(monkeypatch, tmp_path, clean_home) -> None:
    monkeypatch.setattr(config.sys, "platform", "linux")
    assert application_home() == clean_home / ".config" / "benchling-tools"
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    assert application_home() == tmp_path / "xdg" / "benchling-tools"


def test_macos_uses_application_support(monkeypatch, clean_home) -> None:
    monkeypatch.setattr(config.sys, "platform", "darwin")
    monkeypatch.setenv("XDG_CONFIG_HOME", "/ignored")
    assert application_home() == (
        clean_home / "Library" / "Application Support" / "benchling-tools"
    )


def test_windows_uses_appdata_with_profile_fallback(monkeypatch, tmp_path, clean_home) -> None:
    windows = types.SimpleNamespace(name="nt", getenv=os.getenv)
    monkeypatch.setattr(config, "os", windows)
    assert application_home() == clean_home / "AppData" / "Roaming" / "benchling-tools"
    monkeypatch.setenv("APPDATA", str(tmp_path / "roaming"))
    assert application_home() == tmp_path / "roaming" / "benchling-tools"


def test_store_layout_follows_root(tmp_path) -> None:
    store = ConfigStore(tmp_path / "root")
    assert (store.plans_dir, store.journals_dir, store.locks_dir) == (
        tmp_path / "root" / "plans",
        tmp_path / "root" / "journals",
        tmp_path / "root" / "locks",
    )
    store.ensure()
    assert sorted(path.name for path in store.root.iterdir()) == ["journals", "locks", "plans"]


def test_default_store_uses_application_home(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("BENCHLING_TOOLS_HOME", str(tmp_path / "configured"))
    assert ConfigStore().root == (tmp_path / "configured").resolve()


@pytest.mark.skipif(os.name == "nt", reason="POSIX permission bits")
def test_ensure_creates_private_directories_and_tightens_existing_ones(tmp_path) -> None:
    root = tmp_path / "nested" / "app"
    root.mkdir(parents=True, mode=0o755)
    root.chmod(0o755)
    store = ConfigStore(root)
    store.ensure()
    store.ensure()
    for directory in (root, store.plans_dir, store.journals_dir, store.locks_dir):
        assert stat.S_IMODE(directory.stat().st_mode) == 0o700, directory


def test_ensure_tolerates_filesystems_that_reject_chmod(monkeypatch, tmp_path) -> None:
    def refuse(self, mode, *args, **kwargs):
        raise PermissionError("not permitted")

    monkeypatch.setattr(Path, "chmod", refuse)
    store = ConfigStore(tmp_path / "app")
    store.ensure()
    assert store.locks_dir.is_dir()
