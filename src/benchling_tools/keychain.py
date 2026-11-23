"""OS keychain storage for client secrets.

The legacy service name ``benchlingtk`` is retained so existing credentials
remain accessible. The username is the environment name. There is no plaintext
file fallback.
"""

from __future__ import annotations

import os
import shlex
import shutil
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any, Protocol

import keyring
from keyring.errors import KeyringError, KeyringLocked, PasswordDeleteError

from .errors import AuthenticationError
from .redact import register_secret

SERVICE_NAME = "benchlingtk"
_SECRET_SERVICE_NAME = "org.freedesktop.secrets"
_UNLOCK = "gnome-keyring-daemon --unlock --components=secrets"
_PLACEHOLDER_ENV = "dev"
_PLACEHOLDER_URL = "https://your-tenant.benchling.com"
_PLACEHOLDER_CLIENT = "CLIENT"
_INSTALL = "sudo apt-get install -y gnome-keyring dbus"
_README = "See the README section Headless Linux."
# Shown only for `benchling-tools mcp serve`. CLI login already has a terminal session.
_MCP_ENV = (
    "The standard MCP SDK launches stdio servers with only HOME, LOGNAME, PATH, "
    "SHELL, TERM and USER, so DBUS_SESSION_BUS_ADDRESS is missing. "
    "Set it in the MCP client config env block. Cron needs that same long-lived bus exported."
)
# Set for the lifetime of run_mcp_server, including its keychain check.
_in_mcp_serve: ContextVar[bool] = ContextVar("benchling_in_mcp_serve", default=False)


@dataclass(frozen=True)
class _LoginFix:
    """Values already known when a keychain error interrupts login."""

    env: str | None = None
    tenant_url: str | None = None
    client_id: str | None = None


# Set only around `benchling-tools auth login`. Other commands keep the placeholders.
_login_fix: ContextVar[_LoginFix | None] = ContextVar("benchling_login_fix", default=None)
NO_KEYRING_MESSAGE = (
    "No usable OS keychain is available because no Secret Service backend is installed.\n"
    f"{_INSTALL}\n"
    f"{_README}"
)


class _Backend(Protocol):
    priority: float

    def get_password(self, service: str, username: str) -> str | None: ...

    def set_password(self, service: str, username: str, password: str) -> None: ...

    def delete_password(self, service: str, username: str) -> None: ...


def _backend_name(backend: object) -> str:
    kind = type(backend)
    return f"{kind.__module__}.{kind.__qualname__}"


def _usable(backend: Any) -> bool:
    """True when the backend is a real OS keychain, not fail/null/plaintext."""
    kind = type(backend)
    module = kind.__module__.lower()
    name = kind.__name__.lower()
    if module.endswith(".fail") or module.endswith(".null"):
        return False
    if "plaintext" in module or "plaintext" in name:
        return False
    try:
        priority = float(backend.priority)
    except (AttributeError, TypeError, ValueError, RuntimeError):
        return False
    return priority >= 1


def provider_installed() -> bool:
    """True when a Linux Secret Service provider binary is on PATH."""
    return any(
        shutil.which(name)
        for name in ("gnome-keyring-daemon", "kwalletd6", "kwalletd5", "kwalletd")
    )


def secret_service_reachable() -> bool:
    """True when this session bus owns org.freedesktop.secrets."""
    if not os.environ.get("DBUS_SESSION_BUS_ADDRESS"):
        return False
    try:
        connection = _open_session_bus()
    except Exception:
        return False
    try:
        return _secret_service_owned(connection)
    except Exception:
        return False
    finally:
        _close_bus(connection)


def _open_session_bus() -> Any:
    from jeepney.io.blocking import open_dbus_connection  # type: ignore[import-untyped]

    return open_dbus_connection(bus="SESSION", auth_timeout=1.0)


def _secret_service_owned(connection: Any) -> bool:
    from jeepney.bus_messages import message_bus  # type: ignore[import-untyped]
    from jeepney.io.blocking import Proxy

    owned: Any = Proxy(message_bus, connection).NameHasOwner(_SECRET_SERVICE_NAME)
    value = owned[0] if isinstance(owned, tuple) else owned
    return bool(value)


def _close_bus(connection: Any) -> None:
    close = getattr(connection, "close", None)
    if not callable(close):
        return
    try:
        close()
    except Exception:
        return


def _is_locked(exc: BaseException | None) -> bool:
    if exc is None:
        return False
    if isinstance(exc, KeyringLocked):
        return True
    return "lock" in type(exc).__name__.lower()


def classify_keychain_failure(backend: object | None, exc: BaseException | None = None) -> str:
    """Return no_backend, dbus_unreachable, or locked."""
    if _is_locked(exc):
        return "locked"
    if sys.platform.startswith("linux"):
        if not provider_installed():
            return "no_backend"
        if not secret_service_reachable():
            return "dbus_unreachable"
        if exc is not None:
            return "locked"
    return "no_backend"


def _dbus_bus_line() -> str:
    """Describe the session bus that failed. Do not call an unset bus 'set', or the reverse."""
    if os.environ.get("DBUS_SESSION_BUS_ADDRESS"):
        return f"DBUS_SESSION_BUS_ADDRESS is set, but this bus has no {_SECRET_SERVICE_NAME} name."
    return "DBUS_SESSION_BUS_ADDRESS is unset."


def _lines(*parts: str) -> str:
    return "\n".join(part for part in parts if part)


def _cause(cause: str) -> str:
    if cause == "locked":
        return "No usable OS keychain is available because the Secret Service collection is locked."
    if cause == "dbus_unreachable":
        return (
            "No usable OS keychain is available because Secret Service is not reachable on D-Bus. "
            + _dbus_bus_line()
        )
    return "No usable OS keychain is available because no Secret Service backend is installed."


def _shell_value(value: str | None, placeholder: str) -> str:
    """Quote a supplied flag value. An omitted value stays a shell-safe placeholder."""
    if value is None or not value.strip():
        return placeholder
    return shlex.quote(value.strip())


def dbus_login_command(
    *,
    env: str | None = None,
    tenant_url: str | None = None,
    client_id: str | None = None,
) -> str:
    """Unlock the keyring and log in inside one ``dbus-run-session``.

    ``env``, ``tenant_url``, and ``client_id`` are shell-quoted. Omitted values
    stay placeholders. ``$KEYRING_PASSWORD`` and ``$BENCHLING_CLIENT_SECRET``
    stay variable references; this command never contains a secret value.
    """
    login = (
        'printf "%s" "$BENCHLING_CLIENT_SECRET" | benchling-tools auth login '
        f"--env {_shell_value(env, _PLACEHOLDER_ENV)} "
        f"--tenant-url {_shell_value(tenant_url, _PLACEHOLDER_URL)} "
        f"--client-id {_shell_value(client_id, _PLACEHOLDER_CLIENT)} "
        "--secret-stdin"
    )
    # The session exits with its shell, and the bus exits with it. Login has to
    # be in that same shell or the secret is stored on a bus that is already gone.
    script = f'printf "%s\\n" "$KEYRING_PASSWORD" | {_UNLOCK}; {login}'
    return f"dbus-run-session -- sh -c {shlex.quote(script)}"


@contextmanager
def bind_login_fix(
    *,
    env: str | None,
    tenant_url: str | None,
    client_id: str | None,
) -> Iterator[None]:
    """Use these login values in a keychain error raised inside the block."""
    token = _login_fix.set(_LoginFix(env=env, tenant_url=tenant_url, client_id=client_id))
    try:
        yield
    finally:
        _login_fix.reset(token)


def _fix_command(cause: str) -> str:
    """One command. The README has the rest, including a long-lived bus."""
    if cause == "no_backend":
        return _INSTALL
    current = _login_fix.get()
    if current is None:
        return dbus_login_command()
    return dbus_login_command(
        env=current.env,
        tenant_url=current.tenant_url,
        client_id=current.client_id,
    )


def keychain_message(cause: str) -> str:
    """Cause, one fix command, and a README pointer. MCP adds the env-block line.

    Never includes secret values. The MCP line is only present while `mcp serve` is running.
    """
    parts = [_cause(cause), _fix_command(cause), _README]
    if _in_mcp_serve.get():
        parts.append(_MCP_ENV)
    return _lines(*parts)


def _keychain_error(backend: object | None, exc: BaseException | None) -> AuthenticationError:
    cause = classify_keychain_failure(backend, exc)
    if backend is not None:
        backend_name: str = _backend_name(backend)
    elif exc is not None:
        backend_name = type(exc).__name__
    else:
        backend_name = "unknown"
    return AuthenticationError(
        keychain_message(cause),
        details={"backend": backend_name, "cause": cause},
    )


class Keychain:
    """Read and write one environment's client secret."""

    def __init__(self, backend: _Backend | None = None) -> None:
        self._backend = backend

    def backend(self) -> Any:
        """Return the injected backend, or the process-wide keyring backend."""
        if self._backend is not None:
            return self._backend
        return keyring.get_keyring()

    def require(self) -> None:
        """Raise AuthenticationError when no OS keychain can store a secret."""
        backend = self.backend()
        if _usable(backend):
            return
        raise _keychain_error(backend, None)

    def get(self, env: str) -> str | None:
        """Return the stored secret, or None when this environment has none."""
        self.require()
        try:
            value = self.backend().get_password(SERVICE_NAME, env)
        except KeyringError as exc:
            raise _unavailable(exc) from exc
        if isinstance(value, str) and value:
            register_secret(value)
            return value
        return None

    def set(self, env: str, secret: str) -> None:
        """Store a client secret. The value is not logged."""
        self.require()
        register_secret(secret)
        try:
            self.backend().set_password(SERVICE_NAME, env, secret)
        except KeyringError as exc:
            raise _unavailable(exc) from exc

    def delete(self, env: str) -> None:
        """Remove a client secret. Missing entries are not an error."""
        self.require()
        try:
            self.backend().delete_password(SERVICE_NAME, env)
        except PasswordDeleteError:
            return
        except KeyringError as exc:
            raise _unavailable(exc) from exc


def _unavailable(exc: KeyringError) -> AuthenticationError:
    return _keychain_error(None, exc)
