"""Async Benchling HTTP client."""

from __future__ import annotations

import asyncio
import random
from collections.abc import AsyncIterator
from typing import Any

import httpx

from .auth import AuthManager
from .errors import (
    AccessError,
    ApiDriftError,
    AuthenticationError,
    ConflictError,
    NotFoundError,
    PartialCompletionError,
    UpstreamError,
    ValidationError,
)
from .models import Environment
from .redact import redact_text
from .tls import verified_context


class APIPayload(dict[str, Any]):
    """JSON object from Benchling, with the HTTP status kept beside the body.

    The status is an attribute, not a key, so journals and callers that treat the
    payload as the response body do not see it unless they read ``status_code``.
    """

    status_code: int

    def __init__(self, body: dict[str, Any], status_code: int) -> None:
        super().__init__(body)
        self.status_code = status_code


class BenchlingAPI:
    """Small V2/V3 client with read-only retries and normalized errors."""

    def __init__(
        self,
        environment: Environment,
        auth: AuthManager,
        *,
        http: httpx.AsyncClient | None = None,
        download_transport: httpx.AsyncBaseTransport | None = None,
        max_read_attempts: int = 3,
    ) -> None:
        self.environment = environment
        self.auth = auth
        self.http = http or httpx.AsyncClient(timeout=30.0, verify=verified_context())
        self.download_transport = download_transport
        self._owns_http = http is None
        self.max_read_attempts = max_read_attempts

    async def close(self) -> None:
        """Close the HTTP client only if this instance created it."""
        if self._owns_http:
            await self.http.aclose()

    async def request(
        self,
        method: str,
        path: str,
        *,
        version: int = 3,
        params: dict[str, Any] | None = None,
        json: Any = None,
        early_access: bool | None = None,
    ) -> APIPayload:
        """Send one request and return the decoded JSON object.

        Only GET is retried. A non-GET with no confirmed response raises
        PartialCompletionError because the write may have been applied.
        """
        method = method.upper()
        if not path.startswith("/"):
            raise ValidationError("API path must start with '/'")
        base = f"{self.environment.base_url}/api/v{version}"
        headers = {"Accept": "application/json", **self.auth.headers(self.environment)}
        if version == 3 and early_access is not False:
            headers["EARLY-ACCESS"] = "true"
        attempts = self.max_read_attempts if method == "GET" else 1
        response: httpx.Response | None = None
        for attempt in range(attempts):
            try:
                response = await self.http.request(
                    method, f"{base}{path}", params=params, json=json, headers=headers
                )
            except (httpx.TimeoutException, httpx.NetworkError) as exc:
                if method != "GET" or attempt + 1 >= attempts:
                    error_cls = PartialCompletionError if method != "GET" else UpstreamError
                    raise error_cls(
                        "Benchling request did not produce a confirmed response. Check the tenant URL.",
                        details={"method": method, "path": path, "reason": redact_text(str(exc))},
                        retryable=method == "GET",
                    ) from exc
                await self._backoff(attempt, None)
                continue
            if (
                method == "GET"
                and response.status_code in {408, 429, 500, 502, 503, 504}
                and attempt + 1 < attempts
            ):
                await self._backoff(attempt, response)
                continue
            break
        assert response is not None
        return self._decode(response, method=method, path=path)

    async def paginate(
        self,
        path: str,
        *,
        version: int = 3,
        params: dict[str, Any] | None = None,
        item_key: str = "items",
        max_items: int | None = None,
    ) -> AsyncIterator[dict[str, Any]]:
        """Yield items across nextToken pages, stopping at max_items.

        Raises UpstreamError on a malformed page or a repeated token.
        """
        query = dict(params or {})
        yielded = 0
        seen_tokens: set[str] = set()
        while True:
            payload = await self.request("GET", path, version=version, params=query)
            items = payload.get(item_key)
            if not isinstance(items, list):
                raise UpstreamError(
                    "Benchling pagination response has an unexpected shape",
                    details={"path": path, "expected_key": item_key, "keys": sorted(payload)},
                )
            for item in items:
                if not isinstance(item, dict):
                    raise UpstreamError("Benchling returned a non-object list item")
                yield item
                yielded += 1
                if max_items is not None and yielded >= max_items:
                    return
            token = payload.get("nextToken")
            if not token:
                return
            if token in seen_tokens:
                raise UpstreamError(
                    "Benchling returned a repeated pagination token",
                    details={"path": path},
                )
            seen_tokens.add(token)
            query["nextToken"] = token

    async def _backoff(self, attempt: int, response: httpx.Response | None) -> None:
        delay: float | None = None
        if response is not None:
            raw = response.headers.get("Retry-After")
            try:
                delay = min(float(raw), 10.0) if raw else None
            except ValueError:
                delay = None
        if delay is None:
            delay = min(0.25 * (2**attempt) + random.random() * 0.1, 2.0)
        await asyncio.sleep(delay)

    def _decode(self, response: httpx.Response, *, method: str, path: str) -> APIPayload:
        payload: Any
        if response.content:
            try:
                payload = response.json()
            except ValueError:
                payload = {"body": response.text[:2000]}
        else:
            payload = {}
        details = {
            "status": response.status_code,
            "method": method,
            "path": path,
            "response": payload,
            "request_id": response.headers.get("X-Request-ID"),
        }
        if response.status_code in {200, 201, 202, 204}:
            if not isinstance(payload, dict):
                raise UpstreamError(
                    "Benchling returned an unexpected response shape", details=details
                )
            return APIPayload(payload, response.status_code)
        if response.status_code == 400:
            raise ValidationError("Benchling rejected the request", details=details)
        if response.status_code == 401:
            raise AuthenticationError("Benchling authentication failed", details=details)
        if response.status_code == 403:
            raise AccessError("Benchling denied access", details=details)
        if response.status_code == 404:
            raise NotFoundError("Benchling resource was not found", details=details)
        if response.status_code == 405:
            allow = response.headers.get("Allow")
            if allow:
                details["allow"] = allow
            raise ApiDriftError(
                "This endpoint no longer supports this method. The Benchling API has drifted from this client.",
                details=details,
            )
        if response.status_code == 409:
            raise ConflictError("The Benchling resource changed since it was read", details=details)
        if response.status_code == 429:
            raise UpstreamError("Benchling rate limit exceeded", details=details, retryable=True)
        if response.status_code >= 500 and method != "GET":
            raise PartialCompletionError(
                "Benchling returned a server error after receiving a write request; completion is unknown",
                details=details,
            )
        if response.status_code >= 500:
            raise UpstreamError(
                "Benchling returned a server error", details=details, retryable=True
            )
        raise UpstreamError("Unexpected Benchling response", details=details)
