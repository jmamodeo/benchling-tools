"""Local application directories for plans, journals and locks."""

from __future__ import annotations

import os
import sys
from pathlib import Path


def application_home() -> Path:
    """Return the per-user config root; BENCHLING_TOOLS_HOME overrides the platform default."""
    override = os.getenv("BENCHLING_TOOLS_HOME")
    if override:
        return Path(override).expanduser().resolve()
    if os.name == "nt":
        root = Path(os.getenv("APPDATA", Path.home() / "AppData" / "Roaming"))
    elif sys.platform == "darwin":
        root = Path.home() / "Library" / "Application Support"
    else:
        root = Path(os.getenv("XDG_CONFIG_HOME", Path.home() / ".config"))
    return root / "benchling-tools"


class ConfigStore:
    """Locates the on-disk state directories under a single root."""

    def __init__(self, root: Path | None = None) -> None:
        self.root = root or application_home()

    @property
    def plans_dir(self) -> Path:
        """Directory holding saved change plans."""
        return self.root / "plans"

    @property
    def journals_dir(self) -> Path:
        """Directory holding write-execution journals."""
        return self.root / "journals"

    @property
    def locks_dir(self) -> Path:
        """Directory holding lock files."""
        return self.root / "locks"

    @property
    def uploads_dir(self) -> Path:
        """Directory holding the local file references of planned blob uploads.

        It is created on first use by the blob planner, not by ``ensure``.
        """
        return self.root / "uploads"

    def ensure(self) -> None:
        """Create all directories owner-only (0700); chmod failures are ignored."""
        for directory in (
            self.root,
            self.plans_dir,
            self.journals_dir,
            self.locks_dir,
        ):
            directory.mkdir(mode=0o700, parents=True, exist_ok=True)
            try:
                directory.chmod(0o700)
            except OSError:
                pass
