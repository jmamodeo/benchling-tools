"""Named environments stored in environments.json.

The file holds the tenant URL, client ID and protected flag only. Client
secrets live in the OS keychain. ``BENCHLING_<ENV>_*`` variables are not read.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any

from pydantic import ValidationError as ModelValidationError

from .config import ConfigStore
from .errors import ValidationError
from .models import AuthMethod, Environment, explain_environment_error

ENV_NAME = re.compile(r"^[a-z][a-z0-9]{0,31}$")
ENV_NAME_RULE = "use lowercase letters and digits, starting with a letter, at most 32 characters"
FILE_VERSION = 1


def invalid_environment_name(name: str, *, available: list[str] | None = None) -> ValidationError:
    """Explain the environment-name rule. The submitted name is not treated as a secret."""
    shown = f" {name!r}" if name and len(name) <= 64 and name.isprintable() else ""
    return ValidationError(
        f"Invalid environment name{shown}: {ENV_NAME_RULE}.",
        details={"env": None, "available_envs": list(available or [])},
    )


def protection_summary(environment: Environment) -> str:
    """One line describing what a write to this environment requires."""
    name = environment.name
    if environment.protected:
        return (
            f"protected: `changes apply` needs `--env {name} --confirm-env {name}`; "
            f"MCP `changes_apply` needs `--allow-writes {name}`"
        )
    return (
        f"not protected: `changes apply` needs `--env {name}`; "
        f"MCP `changes_apply` needs `--allow-writes {name}`"
    )


def check_apply_allowed(
    environment: Environment, *, env_on_command_line: bool, confirm_env: str | None
) -> None:
    """Refuse a CLI apply to a protected environment unless it is named and confirmed.

    The environment must come from ``--env`` on the command line, and
    ``--confirm-env`` must repeat the same name. A ``--confirm-env`` that names
    another environment is refused even when the target is not protected.
    """
    name = environment.name
    details: dict[str, Any] = {"env": name, "protected": environment.protected}
    if confirm_env is not None and confirm_env != name:
        raise ValidationError(
            f"--confirm-env does not match --env {name}; nothing was applied",
            details=details,
        )
    if not environment.protected:
        return
    if not env_on_command_line:
        raise ValidationError(
            f"Environment {name} is protected; name it with --env {name} on the command line",
            details=details,
        )
    if confirm_env is None:
        raise ValidationError(
            f"Environment {name} is protected; nothing was applied. To write, rerun "
            f"`benchlingtk --env {name} changes apply PLAN_ID --confirm-env {name}`",
            details=details,
        )


def write_private(path: Path, text: str) -> None:
    """Atomically replace a file and force mode 0600, ignoring chmod failures."""
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    temporary = path.parent / f".{path.name}.tmp"
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        try:
            handle = os.fdopen(fd, "w", encoding="utf-8")
        except Exception:
            os.close(fd)
            raise
        with handle:
            handle.write(text)
        try:
            os.chmod(temporary, 0o600)
        except OSError:
            pass
        os.replace(temporary, path)
    except Exception:
        if temporary.exists():
            temporary.unlink()
        raise
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


class EnvironmentConfigStore(ConfigStore):
    """Resolve environments from environments.json under the state directory.

    The inherited directories hold write plans and journals, not secrets.
    Files saved by previous versions are never read.
    """

    @property
    def environments_path(self) -> Path:
        """Path of the non-secret environment file."""
        return self.root / "environments.json"

    def configured_names(self) -> list[str]:
        """Sorted environment names saved in environments.json."""
        return sorted(self._records())

    def resolve(self, name: str | None) -> Environment:
        """Build the Environment for a name.

        Raises ValidationError if the name is missing, unknown, or the saved
        record is invalid. The client secret is not loaded here.
        """
        names = self.configured_names()
        if not name:
            raise ValidationError(
                "An environment is required; pass --env NAME",
                details={"available_envs": names},
            )
        if not ENV_NAME.match(name):
            raise invalid_environment_name(name, available=names)
        records = self._records()
        if name not in records:
            raise ValidationError(
                f"Unknown environment {name!r}; run `benchlingtk auth login --env {name}`",
                details={"env": name, "available_envs": names},
            )
        return self._environment(name, records[name])

    def metadata(self) -> dict[str, Any]:
        """Non-secret JSON description of every configured environment, keyed by name."""
        return {
            env: self.resolve(env).model_dump(mode="json", exclude_none=True)
            for env in self.configured_names()
        }

    def upsert(self, environment: Environment) -> None:
        """Insert or replace one environment's non-secret record."""
        if not environment.client_id:
            raise ValidationError("Client ID is required", details={"env": environment.name})
        records = self._records() if self.environments_path.exists() else {}
        records[environment.name] = {
            "tenant_url": environment.base_url,
            "client_id": environment.client_id,
            "protected": environment.protected,
        }
        self._write(records)

    def saved_protection(self, name: str) -> bool:
        """Saved protected flag, read without validating the rest of the record.

        Any value other than a missing key or ``false`` counts as protected, so a
        damaged flag fails closed.
        """
        if not self.environments_path.exists():
            return False
        record = self._records().get(name)
        if record is None:
            return False
        return record.get("protected", False) is not False

    def set_protected(self, name: str, protected: bool) -> Environment:
        """Mark or unmark one saved environment as protected and return the updated record."""
        environment = self.resolve(name).model_copy(update={"protected": protected})
        self.upsert(environment)
        return environment

    def remove(self, name: str) -> bool:
        """Remove one environment. Returns True when a record was deleted."""
        if not self.environments_path.exists():
            return False
        records = self._records()
        if name not in records:
            return False
        del records[name]
        self._write(records)
        return True

    def _records(self) -> dict[str, Any]:
        path = self.environments_path
        if not path.exists():
            return {}
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ValidationError(
                "Could not read environments.json",
                details={"path": str(path)},
            ) from exc
        if not isinstance(raw, dict) or raw.get("version") != FILE_VERSION:
            raise ValidationError("environments.json has an unsupported version")
        environments = raw.get("environments")
        if not isinstance(environments, dict):
            raise ValidationError("environments.json is invalid")
        for name in environments:
            if not isinstance(name, str) or not ENV_NAME.match(name):
                raise ValidationError("environments.json contains an invalid environment name")
            if not isinstance(environments[name], dict):
                raise ValidationError(
                    "environments.json is invalid",
                    details={"env": name},
                )
        return environments

    def _environment(self, name: str, record: dict[str, Any]) -> Environment:
        client_id = record.get("client_id")
        if (
            not isinstance(client_id, str)
            or not client_id.strip()
            or any(character in client_id for character in "\r\n\x00")
        ):
            raise ValidationError("Invalid client ID", details={"env": name, "field": "client_id"})
        protected = record.get("protected", False)
        if not isinstance(protected, bool):
            raise ValidationError(
                "Invalid protected flag; expected true or false",
                details={"env": name, "field": "protected"},
            )
        tenant_url = record.get("tenant_url")
        if not isinstance(tenant_url, str) or not tenant_url.strip():
            raise ValidationError(
                "Invalid tenant URL. Expected https://host, a bare host, or a host ending in "
                "/api/v2 or /api/v3.",
                details={"env": name, "field": "tenant_url"},
            )
        try:
            environment = Environment.model_validate(
                {
                    "name": name,
                    "tenant_url": tenant_url,
                    "client_id": client_id,
                    "auth_method": AuthMethod.CLIENT_CREDENTIALS,
                    "protected": protected,
                }
            )
        except ModelValidationError as exc:
            # Pydantic errors include input values; never expose them.
            raise explain_environment_error(exc, env=name) from exc
        return environment

    def _write(self, records: dict[str, Any]) -> None:
        self.ensure()
        payload = {"version": FILE_VERSION, "environments": records}
        write_private(self.environments_path, json.dumps(payload, indent=2) + "\n")
