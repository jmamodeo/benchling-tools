"""Locate the SKILL.md that ships with the package. Read-only and offline."""

from __future__ import annotations

import os
import sys
import sysconfig
from pathlib import Path

from .errors import NotFoundError

SKILL_RELATIVE_PATH = Path("share", "benchling-tools", "SKILL.md")
_REPO_ROOT = Path(__file__).resolve().parents[2]


def _data_roots() -> list[Path]:
    """Install prefixes that may hold the data-files, most specific first."""
    roots: list[str] = []
    data = sysconfig.get_paths().get("data")
    if data:
        roots.append(data)
    roots.extend([sys.prefix, sys.base_prefix])
    try:
        user_scheme = sysconfig.get_preferred_scheme("user")
        user_data = sysconfig.get_paths(scheme=user_scheme).get("data")
    except (KeyError, ValueError):  # pragma: no cover - platform without a user scheme
        user_data = None
    if user_data:
        roots.append(user_data)
    unique: list[Path] = []
    for root in roots:
        path = Path(root)
        if path not in unique:
            unique.append(path)
    return unique


def skill_candidates() -> list[Path]:
    """Every location checked, in order: installed data-files, then the source checkout."""
    candidates = [root / SKILL_RELATIVE_PATH for root in _data_roots()]
    candidates.append(_REPO_ROOT / "SKILL.md")
    return candidates


def find_skill_md() -> Path:
    """Return the absolute path of SKILL.md, or raise NotFoundError listing where it looked.

    The source-checkout fallback covers editable and development installs; it is used
    only when the checkout's pyproject.toml is next to it.
    """
    for candidate in skill_candidates():
        if candidate.parent == _REPO_ROOT and not (_REPO_ROOT / "pyproject.toml").is_file():
            continue
        if candidate.is_file():
            return Path(os.path.abspath(candidate))
    raise NotFoundError(
        "SKILL.md was not found. Reinstall benchling-tools with pip, or run from a source checkout.",
        details={"searched": [str(path) for path in skill_candidates()]},
    )


def read_skill_md() -> str:
    """Return the text of the packaged SKILL.md."""
    return find_skill_md().read_text(encoding="utf-8")
