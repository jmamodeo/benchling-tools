"""results get, list, pending and verify accept and return both V2 and V3 result ids."""

from __future__ import annotations

import json

import pytest
from conftest import plain_stdout, runtime_for
from fake_benchling import ENTRY_ID
from typer.testing import CliRunner

from benchlingtk import cli
from benchlingtk.errors import NotFoundError, ValidationError

TABLE = "strtbl_example"
MISSING_V2 = "00000000-0000-4000-8000-0000000000ff"


def record(ct=12):
    return {"schemaId": "assaysch_abc123", "fields": {"ct": {"value": ct}}}


async def uploaded(runtime, fake, count=2):
    planned = await runtime.planner.plan(
        ENTRY_ID, "results", {"table_id": TABLE, "assay_results": [record(n) for n in range(count)]}
    )
    await runtime.planner.apply(planned.id)
    v2_ids = fake.complete_upload()
    v3_ids = [item["v3Id"] for item in fake.assay_results]
    return planned, v2_ids, v3_ids


def calls(fake, path):
    return [call for call in fake.calls if call.path == path]


def v3_ids_sent_to_bulk_get(fake):
    return [
        item
        for call in calls(fake, "/api/v2/assay-results:bulk-get")
        for item in call.params["assayResultIds"].split(",")
        if item.startswith("result_")
    ]


async def test_get_by_v3_id_returns_both_ids(runtime, fake) -> None:
    _plan, v2_ids, v3_ids = await uploaded(runtime, fake)

    data = await runtime.service.get_result_by_any_id(v3_ids[1])

    assert data["id"] == data["v3_id"] == v3_ids[1]
    assert data["v2_id"] == v2_ids[1]
    (lookup,) = calls(fake, "/api/v2/assay-results")
    assert lookup.params["schemaId"] == "assaysch_abc123"
    assert (
        lookup.params["createdAt.gte"] <= "2026-11-19T00:00:02Z" <= lookup.params["createdAt.lte"]
    )
    assert v3_ids_sent_to_bulk_get(fake) == []


async def test_get_by_v2_uuid_maps_to_the_v3_record(runtime, fake) -> None:
    _plan, v2_ids, v3_ids = await uploaded(runtime, fake)

    data = await runtime.service.get_result_by_any_id(v2_ids[0])

    assert data["id"] == data["v3_id"] == v3_ids[0]
    assert data["v2_id"] == v2_ids[0]
    assert [call.path for call in fake.calls[-2:]] == [
        "/api/v2/assay-results:bulk-get",
        f"/api/v3/result/{v3_ids[0]}",
    ]


async def test_get_leaves_v2_id_null_when_benchling_does_not_return_it(runtime, fake) -> None:
    data = await runtime.service.get_result_by_any_id("result_2")
    assert data["id"] == data["v3_id"] == "result_2"
    assert data["v2_id"] is None


@pytest.mark.parametrize(
    ("result_id", "namespace", "expected", "lookup"),
    [
        (MISSING_V2, "v2_assay_result", "v2_assay_result", "V2 GET /assay-results:bulk-get"),
        ("result_zzz", "v3_result", "v3_result", "V3 GET /result/{result_id}"),
        ("legacy_zzz", "unknown", "v3_result", "V3 GET /result/{result_id}"),
    ],
)
async def test_get_names_the_namespace_when_an_id_cannot_be_read(
    runtime, fake, result_id, namespace, expected, lookup
) -> None:
    with pytest.raises(NotFoundError) as caught:
        await runtime.service.get_result_by_any_id(result_id)

    details = caught.value.details
    assert details["result_id"] == result_id
    assert details["id_namespace"] == namespace
    assert details["expected_id_namespace"] == expected
    assert details["lookup"] == lookup
    assert details["accepted_id_namespaces"] == ["v3_result", "v2_assay_result"]
    assert expected in caught.value.message


async def test_list_returns_both_ids_and_takes_either_namespace(runtime, fake) -> None:
    _plan, v2_ids, v3_ids = await uploaded(runtime, fake, count=3)

    unfiltered = await runtime.service.list_results_by_any_id()
    by_v3 = {item["v3_id"]: item["v2_id"] for item in unfiltered["items"]}
    assert by_v3 == {
        "result_1": None,
        "result_2": None,
        "result_3": None,
        **dict(zip(v3_ids, v2_ids, strict=True)),
    }
    assert "not_found" not in unfiltered

    fake.calls.clear()
    data = await runtime.service.list_results_by_any_id(
        result_ids=[v2_ids[0], v3_ids[2], MISSING_V2]
    )

    assert [(item["id"], item["v2_id"]) for item in data["items"]] == [
        (v3_ids[0], v2_ids[0]),
        (v3_ids[2], v2_ids[2]),
    ]
    assert data["not_found"] == [
        {
            "result_id": MISSING_V2,
            "id_namespace": "v2_assay_result",
            "expected_id_namespace": "v2_assay_result",
            "lookup": "V2 GET /assay-results:bulk-get",
        }
    ]
    (listed,) = calls(fake, "/api/v3/result/items")
    assert listed.params["id.anyOf"] == f"{v3_ids[0]},{v3_ids[2]}"
    assert v3_ids_sent_to_bulk_get(fake) == []


async def test_list_with_only_unmapped_ids_does_not_list_everything(runtime, fake) -> None:
    data = await runtime.service.list_results_by_any_id(result_ids=[MISSING_V2])
    assert data["items"] == [] and data["next_token"] is None
    assert data["not_found"][0]["id_namespace"] == "v2_assay_result"
    assert calls(fake, "/api/v3/result/items") == []


async def test_list_refuses_more_than_100_ids(runtime, fake) -> None:
    with pytest.raises(ValidationError, match="at most 100"):
        await runtime.service.list_results_by_any_id(result_ids=[f"result_{n}" for n in range(101)])


async def test_pending_by_ids_takes_v2_and_v3_ids(runtime, fake) -> None:
    _plan, v2_ids, v3_ids = await uploaded(runtime, fake, count=3)

    data = await runtime.planner.read_back_results(
        result_ids=[v3_ids[0], v2_ids[1], v3_ids[2], v2_ids[2], "result_missing"]
    )

    assert [(item["v2_id"], item["v3_id"]) for item in data["items"]] == list(
        zip(v2_ids, v3_ids, strict=True)
    )
    assert all(item["id"] == item["v2_id"] for item in data["items"])
    assert data["counts"]["pending"] == 3 and data["counts"]["not_found"] == 1
    assert data["not_found"] == ["result_missing"]
    assert data["not_found_details"] == [
        {
            "result_id": "result_missing",
            "id_namespace": "v3_result",
            "expected_id_namespace": "v3_result",
            "lookup": "V3 GET /result/{result_id}, then V2 GET /assay-results by schemaId "
            "and createdAt",
        }
    ]
    assert v3_ids_sent_to_bulk_get(fake) == []


async def test_pending_rejection_of_v2_ids_names_the_namespace(runtime, fake) -> None:
    fake.payload_overrides.append(
        (
            "/assay-results:bulk-get",
            400,
            {"error": {"message": "One or more provided assayResultIds were invalid"}},
        )
    )
    with pytest.raises(ValidationError) as caught:
        await runtime.planner.read_back_results(result_ids=["not-a-uuid"])
    details = caught.value.details
    assert details["id_namespace"] == "unknown"
    assert details["expected_id_namespace"] == "v2_assay_result"
    assert details["lookup"] == "V2 GET /assay-results:bulk-get"
    assert details["result_ids"] == ["not-a-uuid"]


async def test_verify_reports_both_ids_from_v2_task_ids(runtime, fake) -> None:
    planned, v2_ids, v3_ids = await uploaded(runtime, fake)

    data = await runtime.planner.verify_results(planned.id)

    assert data["ok"] is True
    assert [(row["result_id"], row["v2_id"], row["v3_id"]) for row in data["needs_accept"]] == [
        (v2, v2, v3) for v2, v3 in zip(v2_ids, v3_ids, strict=True)
    ]


async def test_verify_and_pending_read_a_plan_whose_task_gave_only_v3_ids(runtime, fake) -> None:
    planned, v2_ids, v3_ids = await uploaded(runtime, fake)
    fake.tasks["task_abc123"]["response"]["assayResults"] = [{"v3Id": item} for item in v3_ids]

    verified = await runtime.planner.verify_results(planned.id)
    pending = await runtime.planner.read_back_results(plan_id=planned.id)

    assert verified["ok"] is True and verified["counts"]["matched"] == 2
    assert {row["v3_id"] for row in verified["needs_accept"]} == set(v3_ids)
    assert {row["v2_id"] for row in verified["needs_accept"]} == set(v2_ids)
    assert [item["v3_id"] for item in pending["items"]] == v3_ids
    assert all(item["table_id"] == TABLE for item in pending["items"])
    assert v3_ids_sent_to_bulk_get(fake) == []


@pytest.fixture
def invoke(benchling_env, fake, monkeypatch):
    monkeypatch.setattr(cli, "Runtime", lambda environment: runtime_for(fake, environment))

    def run(*args: str):
        result = CliRunner().invoke(cli.app, [*args, "--env", "sandbox"])
        return result.exit_code, json.loads(result.stdout)

    return run


def test_cli_get_list_and_pending_take_either_id(invoke, fake) -> None:
    fake.assay_results.append(
        {
            "id": "00000000-0000-4000-8000-000000000001",
            "v3Id": "result_up1",
            "entryId": None,
            "createdAt": "2026-11-19T00:00:01Z",
            "schema": {"id": "assaysch_abc123"},
            "fields": {},
        }
    )
    v2_id = "00000000-0000-4000-8000-000000000001"
    for ref in (v2_id, "result_up1"):
        code, payload = invoke("results", "get", ref)
        assert code == 0, payload
        assert (payload["data"]["v2_id"], payload["data"]["v3_id"]) == (v2_id, "result_up1")
        code, payload = invoke("results", "list", "--id", ref)
        assert code == 0, payload
        assert [(i["v2_id"], i["v3_id"]) for i in payload["data"]["items"]] == [
            (v2_id, "result_up1")
        ]
        code, payload = invoke("results", "pending", "--id", ref)
        assert code == 0, payload
        assert [(i["v2_id"], i["v3_id"]) for i in payload["data"]["items"]] == [
            (v2_id, "result_up1")
        ]


def test_cli_get_of_an_unknown_v2_id_is_a_namespaced_not_found(invoke, fake) -> None:
    code, payload = invoke("results", "get", MISSING_V2)
    assert code == 4
    assert payload["ok"] is False
    assert payload["error"]["code"] == "not_found"
    assert payload["error"]["details"]["id_namespace"] == "v2_assay_result"
    assert payload["error"]["details"]["lookup"] == "V2 GET /assay-results:bulk-get"
    assert "v2_assay_result" in payload["error"]["message"]


@pytest.mark.parametrize(
    "path",
    [("results", "get"), ("results", "list"), ("results", "pending"), ("results", "verify")],
)
def test_help_says_both_namespaces_are_accepted_and_returned(path) -> None:
    help_text = plain_stdout([*path, "--help"])
    for token in ("result_", "UUID", "v2_id", "v3_id"):
        assert token in help_text, (path, token)


async def test_get_of_a_v2_record_without_a_v3_id_is_not_found(runtime, fake) -> None:
    fake.assay_results.append({"id": MISSING_V2, "entryId": None, "fields": {}})
    with pytest.raises(NotFoundError, match="has no V3 result id") as caught:
        await runtime.service.get_result_by_any_id(MISSING_V2)
    assert caught.value.details["id_namespace"] == "v2_assay_result"


async def test_get_rejects_an_empty_id(runtime, fake) -> None:
    with pytest.raises(ValidationError, match="non-empty"):
        await runtime.service.get_result_by_any_id("  ")


async def test_v2_id_lookup_pages_and_tolerates_odd_records(runtime, fake) -> None:
    _plan, v2_ids, v3_ids = await uploaded(runtime, fake, count=3)
    fake.assay_result_page_size = 1
    fake.results.append({"id": "result_naive", "schemaId": "assaysch_abc123", "createdAt": "bad"})

    data = await runtime.service.list_results_by_any_id(result_ids=[v3_ids[2], "result_naive"])

    assert {item["id"]: item["v2_id"] for item in data["items"]} == {
        v3_ids[2]: v2_ids[2],
        "result_naive": None,
    }
    assert [call.params.get("nextToken") for call in calls(fake, "/api/v2/assay-results")] == [
        None,
        "1",
    ]


async def test_v2_id_lookup_failure_leaves_v2_id_null(runtime, fake) -> None:
    _plan, _v2_ids, v3_ids = await uploaded(runtime, fake, count=1)
    fake.payload_overrides.append(("/api/v2/assay-results", 403, {"error": {"message": "no"}}))
    data = await runtime.service.get_result_by_any_id(v3_ids[0])
    assert data["v3_id"] == v3_ids[0] and data["v2_id"] is None


async def test_v2_id_lookup_rejects_a_malformed_page(runtime, fake) -> None:
    _plan, _v2_ids, v3_ids = await uploaded(runtime, fake, count=1)
    fake.payload_overrides.append(("/api/v2/assay-results", 200, {"assayResults": "nope"}))
    with pytest.raises(Exception, match="unexpected result list response"):
        await runtime.planner.read_back_results(result_ids=[v3_ids[0]])


async def test_archived_results_are_mapped_from_their_v3_id(runtime, fake) -> None:
    _plan, v2_ids, v3_ids = await uploaded(runtime, fake, count=1)
    fake.assay_results[0]["archiveRecord"] = {"reason": "Retired"}

    data = await runtime.planner.read_back_results(result_ids=[v3_ids[0]])

    assert [(item["v2_id"], item["state"]) for item in data["items"]] == [(v2_ids[0], "archived")]
    (lookup,) = calls(fake, "/api/v2/assay-results")
    assert lookup.params["archiveReason"] == "ANY_ARCHIVED_OR_NOT_ARCHIVED"


async def test_results_created_far_apart_are_looked_up_in_their_own_windows(runtime, fake) -> None:
    _plan, v2_ids, v3_ids = await uploaded(runtime, fake, count=3)
    fake.assay_results[2]["createdAt"] = "2026-11-20T00:00:00Z"

    data = await runtime.planner.read_back_results(result_ids=[v3_ids[0], v3_ids[2]])

    assert [item["v2_id"] for item in data["items"]] == [v2_ids[0], v2_ids[2]]
    windows = [
        (call.params["createdAt.gte"], call.params["createdAt.lte"])
        for call in calls(fake, "/api/v2/assay-results")
    ]
    assert windows == [
        ("2026-11-19T00:00:00Z", "2026-11-19T00:00:02Z"),
        ("2026-11-19T23:59:59Z", "2026-11-20T00:00:01Z"),
    ]
