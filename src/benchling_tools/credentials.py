"""Login, logout, and status for one or more named environments."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import httpx
from pydantic import ValidationError as ModelValidationError

from .auth import AuthManager
from .environment import ENV_NAME, EnvironmentConfigStore, invalid_environment_name, write_private
from .errors import AuthenticationError, BenchlingAgentError, ValidationError
from .keychain import Keychain, bind_login_fix
from .models import AuthMethod, Environment, explain_environment_error
from .redact import redact_text, register_secret
from .tls import verified_context


def open_token_client() -> httpx.Client:
    """HTTP client used only for the OAuth token endpoint."""
    return httpx.Client(timeout=30.0, verify=verified_context())


def build_environment(name: str, tenant_url: str, client_id: str) -> Environment:
    """Validate a tenant URL and client ID separately, without echoing either value."""
    if not ENV_NAME.match(name):
        raise invalid_environment_name(name)
    client_id = client_id.strip()
    if not client_id:
        raise ValidationError("Client ID is required", details={"env": name, "field": "client_id"})
    if any(character in client_id for character in "\r\n\x00"):
        raise ValidationError("Invalid client ID", details={"env": name, "field": "client_id"})
    try:
        environment = Environment.model_validate(
            {
                "name": name,
                "tenant_url": tenant_url.strip(),
                "client_id": client_id,
                "auth_method": AuthMethod.CLIENT_CREDENTIALS,
            }
        )
    except ModelValidationError as exc:
        raise explain_environment_error(exc, env=name) from exc
    return environment


def login_fix_values(
    store: EnvironmentConfigStore,
    name: str,
    tenant_url: str | None,
    client_id: str | None,
) -> tuple[str | None, str | None]:
    """Tenant URL and client ID to print in a keychain fix command.

    A supplied tenant URL is normalized. A supplied client ID is trimmed.
    Either one that was omitted comes from environments.json when ``name`` is
    already configured. A value that cannot be used is left unset.
    """
    saved_url, saved_client = _saved_login_fields(store, name)
    supplied_url = _command_tenant_url(tenant_url) if tenant_url and tenant_url.strip() else None
    supplied_client = client_id.strip() if client_id and client_id.strip() else None
    return supplied_url or saved_url, supplied_client or saved_client


def _command_tenant_url(value: str) -> str | None:
    try:
        return build_environment("dev", value, "client").base_url
    except BenchlingAgentError:
        return None


def _saved_login_fields(store: EnvironmentConfigStore, name: str) -> tuple[str | None, str | None]:
    if not store.environments_path.exists():
        return None, None
    try:
        environment = store.resolve(name)
    except BenchlingAgentError:
        return None, None
    return environment.base_url, environment.client_id


def login(
    store: EnvironmentConfigStore,
    keychain: Keychain,
    *,
    name: str,
    tenant_url: str,
    client_id: str,
    secret: str,
    protected: bool | None = None,
    http: httpx.Client | None = None,
) -> Environment:
    """Verify client credentials, then save the environment and its secret.

    ``protected`` marks the environment for the write guard. None keeps the
    saved flag of an existing environment, so a fresh login never drops it.
    Nothing is written if the token request fails. A failure while saving rolls
    the keychain and environments.json back to their previous contents.
    """
    if secret == "" or secret.strip() == "":
        raise ValidationError("Client secret is required", details={"env": name})
    register_secret(secret)
    environment = build_environment(name, tenant_url, client_id)
    with bind_login_fix(
        env=environment.name,
        tenant_url=environment.base_url,
        client_id=environment.client_id,
    ):
        keychain.require()
        if store.environments_path.exists():
            store.configured_names()
        if protected is None:
            protected = store.saved_protection(name)
        environment = environment.model_copy(update={"protected": protected})
        _verify_token(environment, secret, http)
        previous_secret = keychain.get(name)
        previous_file = _read_file(store)
        try:
            keychain.set(name, secret)
            store.upsert(environment)
        except BenchlingAgentError:
            _rollback(store, keychain, name, previous_secret, previous_file)
            raise
        except Exception as exc:
            _rollback(store, keychain, name, previous_secret, previous_file)
            raise AuthenticationError(
                "Could not save the environment after authentication succeeded",
                details={"env": name, "reason": type(exc).__name__},
            ) from exc
    return environment


def logout(store: EnvironmentConfigStore, keychain: Keychain, name: str) -> None:
    """Remove the environment record and its keychain secret."""
    names = store.configured_names() if store.environments_path.exists() else []
    if not ENV_NAME.match(name):
        raise invalid_environment_name(name, available=names)
    keychain.require()
    present = name in names
    secret = keychain.get(name)
    if not present and secret is None:
        raise ValidationError(
            f"Environment {name!r} is not configured.",
            details={"env": name, "available_envs": names},
        )
    keychain.delete(name)
    if not present:
        return
    try:
        store.remove(name)
    except Exception:
        if secret is not None:
            keychain.set(name, secret)
        raise


@dataclass
class StatusReport:
    """Local configuration for each environment, plus optional token checks."""

    environments: dict[str, dict[str, Any]] = field(default_factory=dict)
    failed_envs: list[str] = field(default_factory=list)


def collect_status(
    store: EnvironmentConfigStore,
    keychain: Keychain,
    *,
    name: str | None,
    check: bool,
    http: httpx.Client | None = None,
) -> StatusReport:
    """Describe saved environments without including secrets or tokens."""
    names = [name] if name else store.configured_names()
    if name:
        store.resolve(name)
    if not names:
        return StatusReport()
    keychain.require()
    secrets = {item: secret for item in names if (secret := keychain.get(item))}
    owns_http = http is None
    client = http or open_token_client()
    auth = AuthManager(secrets, http=client)
    try:
        report = StatusReport()
        for item in names:
            environment = store.resolve(item)
            row = auth.status(environment)
            row["protected"] = environment.protected
            if check:
                row["token_check"] = _token_check(auth, environment, bool(secrets.get(item)))
                row["access_token_cached"] = auth.status(environment)["access_token_cached"]
                if row["token_check"]["ok"] is False:
                    report.failed_envs.append(item)
            report.environments[item] = row
        return report
    finally:
        auth.close()
        if owns_http:
            client.close()


def _verify_token(environment: Environment, secret: str, http: httpx.Client | None) -> None:
    owns_http = http is None
    client = http or open_token_client()
    auth = AuthManager({environment.name: secret}, http=client)
    try:
        auth.headers(environment)
    finally:
        auth.close()
        if owns_http:
            client.close()


def _token_check(
    auth: AuthManager, environment: Environment, secret_present: bool
) -> dict[str, Any]:
    if not secret_present:
        return {
            "ok": False,
            "message": (
                "Client secret is not in the keychain; "
                f"run benchling-tools auth login --env {environment.name}"
            ),
        }
    try:
        auth.headers(environment)
    except BenchlingAgentError as exc:
        return {"ok": False, "message": redact_text(exc.message)}
    return {"ok": True}


def _read_file(store: EnvironmentConfigStore) -> str | None:
    path = store.environments_path
    if not path.exists():
        return None
    return path.read_text(encoding="utf-8")


def _rollback(
    store: EnvironmentConfigStore,
    keychain: Keychain,
    name: str,
    previous_secret: str | None,
    previous_file: str | None,
) -> None:
    try:
        if previous_secret is None:
            keychain.delete(name)
        else:
            keychain.set(name, previous_secret)
        path = store.environments_path
        if previous_file is None:
            if path.exists():
                path.unlink()
        else:
            write_private(path, previous_file)
    except Exception as exc:
        raise AuthenticationError(
            "Saving the environment failed, and the previous credentials could not be restored. "
            "Run `benchling-tools auth login --env NAME` again.",
            details={"env": name, "reason": type(exc).__name__},
        ) from exc
