"""OAuth client-credentials token handling."""

from __future__ import annotations

import threading
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx

from .errors import AuthenticationError, UpstreamError
from .models import Environment, TokenBundle
from .redact import redact_text, register_secret
from .tls import verified_context


class AuthManager:
    """Client-credentials auth with an in-memory, per-environment token cache."""

    def __init__(
        self,
        secrets: Mapping[str, str],
        *,
        http: httpx.Client | None = None,
    ) -> None:
        self.secrets = dict(secrets)
        for secret in self.secrets.values():
            register_secret(secret)
        self.http = http or httpx.Client(timeout=30.0, verify=verified_context())
        self._owns_http = http is None
        self._app_tokens: dict[tuple[str, str, str | None], TokenBundle] = {}
        self._app_lock = threading.Lock()

    def close(self) -> None:
        """Close the HTTP client only if this manager created it."""
        if self._owns_http:
            self.http.close()

    def headers(self, environment: Environment) -> dict[str, str]:
        """Return a Bearer Authorization header, fetching a token if the cache is empty or expired.

        Raises AuthenticationError when credentials are missing or rejected.
        """
        return self._app_headers(environment)

    def status(self, environment: Environment) -> dict[str, Any]:
        """Describe credential state without network calls or exposing secrets."""
        bundle = self._app_tokens.get(
            (environment.name, environment.base_url, environment.client_id)
        )
        secret_present = bool(self.secrets.get(environment.name))
        return {
            "env": environment.name,
            "method": environment.auth_method.value,
            "configured": bool(environment.client_id and secret_present),
            "secret_present": secret_present,
            "tenant_url": environment.base_url,
            "client_id": environment.client_id,
            "credential_source": "keyring",
            "identity": "app",
            "access_token_cached": bundle.usable() if bundle else False,
        }

    def _app_headers(self, environment: Environment) -> dict[str, str]:
        # Client credentials issue no refresh token. A long-lived MCP process
        # requests a new access token on expiry; each CLI has its own cache.
        with self._app_lock:
            cache_key = (environment.name, environment.base_url, environment.client_id)
            bundle = self._app_tokens.get(cache_key)
            if bundle and bundle.usable():
                return {"Authorization": f"Bearer {bundle.access_token}"}
            secret = self.secrets.get(environment.name)
            if not secret or not environment.client_id:
                raise AuthenticationError("App client credentials are missing")
            payload = self._token_request(
                environment,
                {
                    "grant_type": "client_credentials",
                    "client_id": environment.client_id,
                    "client_secret": secret,
                },
            )
            bundle = self._bundle(payload)
            if bundle.token_type.lower() != "bearer":
                raise UpstreamError("Benchling returned an unsupported token type")
            self._app_tokens[cache_key] = bundle
            return {"Authorization": f"Bearer {bundle.access_token}"}

    def _token_request(self, environment: Environment, data: dict[str, str]) -> dict[str, Any]:
        identity = {"env": environment.name, "tenant_url": environment.base_url}
        try:
            response = self.http.post(
                f"{environment.base_url}/oauth/token",
                data=data,
                headers={"Accept": "application/json"},
            )
        except httpx.HTTPError as exc:
            raise UpstreamError(
                "Could not reach the Benchling token endpoint. Check the tenant URL.",
                details={**identity, "reason": redact_text(str(exc))},
                retryable=True,
            ) from exc
        if response.status_code == 401:
            raise AuthenticationError(
                f"Benchling at {environment.base_url} rejected the client ID or secret.",
                details={**identity, "status": response.status_code},
            )
        if response.status_code >= 400:
            raise AuthenticationError(
                "Benchling rejected authentication",
                details={**identity, "status": response.status_code},
            )
        try:
            token_response: dict[str, Any] = response.json()
            return token_response
        except ValueError as exc:
            raise UpstreamError("Benchling returned an invalid token response") from exc

    def _bundle(self, payload: dict[str, Any]) -> TokenBundle:
        if not payload.get("access_token") or not payload.get("expires_in"):
            raise UpstreamError(
                "Benchling returned an incomplete token response",
                details={"keys": sorted(payload)},
            )
        access_token = str(payload["access_token"])
        register_secret(access_token)
        now = datetime.now(UTC)
        return TokenBundle(
            access_token=access_token,
            token_type=payload.get("token_type", "Bearer"),
            expires_at=now + timedelta(seconds=int(payload["expires_in"])),
        )
