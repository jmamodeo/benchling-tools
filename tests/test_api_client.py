from __future__ import annotations

import httpx
import pytest
from conftest import make_api, make_environment

from benchling_tools import api as api_module
from benchling_tools.api import BenchlingAPI
from benchling_tools.auth import AuthManager
from benchling_tools.errors import (
    ApiDriftError,
    PartialCompletionError,
    UpstreamError,
    ValidationError,
)


class Sleeps(list):
    async def sleep(self, delay: float) -> None:
        self.append(delay)


@pytest.fixture
def sleeps(monkeypatch) -> Sleeps:
    recorded = Sleeps()
    monkeypatch.setattr(api_module, "asyncio", recorded_namespace(recorded))
    return recorded


def recorded_namespace(recorded: Sleeps):
    class Namespace:
        sleep = staticmethod(recorded.sleep)

    return Namespace


def retrying_api(handler, attempts: int = 3) -> BenchlingAPI:
    api = make_api(handler)
    api.max_read_attempts = attempts
    return api


def sequence(*responses):
    queue = list(responses)
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        item = queue.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    return handler, calls


async def test_reads_retry_transient_status_and_honor_retry_after(sleeps) -> None:
    handler, calls = sequence(
        httpx.Response(503, headers={"Retry-After": "3"}),
        httpx.Response(429, headers={"Retry-After": "999"}),
        httpx.Response(200, json={"ok": True}),
    )
    api = retrying_api(handler)
    assert await api.request("GET", "/entry/etr_abc123") == {"ok": True}
    assert len(calls) == 3
    assert sleeps == [3.0, 10.0]


async def test_retry_after_that_is_not_numeric_uses_bounded_backoff(sleeps) -> None:
    handler, _ = sequence(
        httpx.Response(502, headers={"Retry-After": "soon"}), httpx.Response(200, json={})
    )
    await retrying_api(handler).request("GET", "/x")
    assert 0.25 <= sleeps[0] <= 0.35


async def test_exhausted_read_retries_raise_retryable_upstream_error(sleeps) -> None:
    handler, calls = sequence(*[httpx.Response(500)] * 3)
    with pytest.raises(UpstreamError) as caught:
        await retrying_api(handler).request("GET", "/x")
    assert caught.value.retryable is True
    assert caught.value.details["status"] == 500
    assert len(calls) == 3 and len(sleeps) == 2


async def test_rate_limit_error_is_retryable_when_attempts_are_exhausted() -> None:
    handler, _ = sequence(httpx.Response(429, json={"message": "slow down"}))
    with pytest.raises(UpstreamError, match="rate limit") as caught:
        await make_api(handler).request("GET", "/x")
    assert caught.value.retryable is True


async def test_network_errors_are_retried_for_reads_then_reported(sleeps) -> None:
    boom = httpx.ConnectError("refused")
    handler, calls = sequence(boom, boom, boom)
    with pytest.raises(UpstreamError) as caught:
        await retrying_api(handler).request("GET", "/x")
    assert caught.value.retryable is True
    assert caught.value.details["method"] == "GET"
    assert len(calls) == 3


async def test_read_recovers_after_a_timeout(sleeps) -> None:
    handler, calls = sequence(httpx.ReadTimeout("slow"), httpx.Response(200, json={"a": 1}))
    assert await retrying_api(handler).request("GET", "/x") == {"a": 1}
    assert len(calls) == 2


async def test_write_timeout_is_unknown_completion_and_never_retried(sleeps) -> None:
    handler, calls = sequence(httpx.ReadTimeout("slow"), httpx.Response(200, json={}))
    with pytest.raises(PartialCompletionError) as caught:
        await retrying_api(handler).request("PATCH", "/x", json={"a": 1})
    assert caught.value.retryable is False
    assert len(calls) == 1 and sleeps == []


@pytest.mark.parametrize("path", ["entry/x", "", "https://evil.example/x"])
async def test_paths_must_be_absolute_api_paths(path) -> None:
    handler, calls = sequence()
    with pytest.raises(ValidationError):
        await make_api(handler).request("GET", path)
    assert calls == []


async def test_request_targets_versioned_base_with_auth_and_early_access_headers() -> None:
    handler, calls = sequence(
        httpx.Response(200, json={}), httpx.Response(200, json={}), httpx.Response(200, json={})
    )
    api = make_api(handler)
    await api.request("get", "/entry/etr_abc123", params={"a": "1"})
    await api.request("GET", "/entries", version=2)
    await api.request("GET", "/entry/etr_abc123", early_access=False)
    first, second, third = calls
    assert str(first.url) == "https://tenant.benchling.com/api/v3/entry/etr_abc123?a=1"
    assert first.headers["Authorization"] == "Bearer token"
    assert first.headers["EARLY-ACCESS"] == "true"
    assert second.url.path == "/api/v2/entries" and "EARLY-ACCESS" not in second.headers
    assert "EARLY-ACCESS" not in third.headers


@pytest.mark.parametrize("status", [200, 201, 202])
async def test_success_statuses_return_json_objects(status) -> None:
    handler, _ = sequence(httpx.Response(status, json={"id": "x"}))
    assert await make_api(handler).request("POST", "/x", json={}) == {"id": "x"}


async def test_empty_success_body_is_an_empty_object() -> None:
    handler, _ = sequence(httpx.Response(204))
    assert await make_api(handler).request("DELETE", "/x") == {}


async def test_non_object_success_body_is_rejected() -> None:
    handler, _ = sequence(httpx.Response(200, json=[1, 2]))
    with pytest.raises(UpstreamError, match="unexpected response shape"):
        await make_api(handler).request("GET", "/x")


async def test_method_not_allowed_is_reported_as_api_drift() -> None:
    handler, _ = sequence(
        httpx.Response(
            405,
            json={"error": {"message": "Method PATCH is not allowed"}},
            headers={"Allow": "GET, HEAD, OPTIONS"},
        )
    )
    with pytest.raises(ApiDriftError, match="no longer supports this method") as caught:
        await make_api(handler).request("PATCH", "/document-part-unstructured-table/docpart_x")
    assert caught.value.code == "api_drift"
    assert caught.value.details["status"] == 405
    assert caught.value.details["allow"] == "GET, HEAD, OPTIONS"
    assert int(caught.value.exit_code) == 7


async def test_non_json_error_body_is_truncated_into_details() -> None:
    handler, _ = sequence(httpx.Response(418, text="t" * 5000, headers={"X-Request-ID": "req_1"}))
    with pytest.raises(UpstreamError, match="Unexpected Benchling response") as caught:
        await make_api(handler).request("GET", "/x")
    assert len(caught.value.details["response"]["body"]) == 2000
    assert caught.value.details["request_id"] == "req_1"
    assert caught.value.details["status"] == 418


async def test_write_5xx_is_unknown_completion() -> None:
    handler, calls = sequence(httpx.Response(504), httpx.Response(200, json={}))
    with pytest.raises(PartialCompletionError):
        await retrying_api(handler).request("POST", "/x", json={})
    assert len(calls) == 1


async def test_paginate_follows_tokens_and_honors_max_items() -> None:
    pages = {
        None: {"items": [{"n": 1}, {"n": 2}], "nextToken": "t1"},
        "t1": {"items": [{"n": 3}], "nextToken": ""},
    }

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=pages[request.url.params.get("nextToken")])

    api = make_api(handler)
    assert [i["n"] async for i in api.paginate("/things")] == [1, 2, 3]
    assert [i["n"] async for i in api.paginate("/things", max_items=2)] == [1, 2]


@pytest.mark.parametrize(
    ("payload", "message"),
    [
        ({"entries": []}, "unexpected shape"),
        ({"items": "nope"}, "unexpected shape"),
        ({"items": ["not-an-object"]}, "non-object"),
        ({"items": [], "nextToken": "loop"}, "repeated pagination token"),
    ],
)
async def test_paginate_rejects_malformed_pages(payload, message) -> None:
    handler, _ = sequence(*[httpx.Response(200, json=payload)] * 3)
    with pytest.raises(UpstreamError, match=message):
        async for _ in make_api(handler).paginate("/things"):
            pass


async def test_owned_http_client_is_closed_but_injected_one_is_not() -> None:
    environment = make_environment()
    auth = AuthManager({environment.name: "secret"})
    owned = BenchlingAPI(environment, auth)
    await owned.close()
    assert owned.http.is_closed
    injected = httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200)))
    shared = BenchlingAPI(environment, auth, http=injected)
    await shared.close()
    assert not injected.is_closed
    await injected.aclose()
    auth.close()
