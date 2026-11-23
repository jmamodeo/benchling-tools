"""Verify an SDK bulk-create payload with the same diff and report as a result plan."""

from __future__ import annotations

import json

import pytest
from conftest import runtime_for
from fake_benchling import ENTRY_ID, entry_web_url
from typer.testing import CliRunner

from benchling_tools import cli
from benchling_tools.errors import ValidationError
from benchling_tools.plans import planner as planner_module
from benchling_tools.plans import readback
from benchling_tools.plans.planner import ChangePlanner

TABLE = "strtbl_example"
SCHEMA = "assaysch_abc123"
OTHER_SCHEMA = "assaysch_other"
AFTER = "2026-11-19T00:00:00Z"
CLIENT_ONE = "11111111-1111-4111-8111-111111111111"
CLIENT_TWO = "22222222-2222-4222-8222-222222222222"


def result(ct, schema=SCHEMA, **extra):
    return {"schemaId": schema, "fields": {"ct": {"value": ct}}, **extra}


def sdk_push(fake, records, table_id=TABLE):
    """Create records the way an SDK client would, outside any toolkit plan."""
    fake.bulk_payloads.append({"tableId": table_id, "assayResults": records})
    return fake.complete_upload()


def list_calls(fake):
    return [call for call in fake.calls if call.path == "/api/v2/assay-results"]


async def test_payload_reports_matched_mismatched_missing_and_extra(runtime, fake) -> None:
    sdk_push(fake, [result(1), result(22)])
    sdk_push(fake, [result(99)])
    body = {"tableId": TABLE, "assayResults": [result(1), result(2), result(3, OTHER_SCHEMA)]}
    writes = len(fake.writes())

    data = await runtime.planner.verify_payload(body, created_after=AFTER)

    assert data["ok"] is False
    assert data["counts"] == {
        "planned": 3,
        "fetched": 3,
        "matched": 1,
        "mismatched": 1,
        "missing": 1,
        "extra": 1,
        "pending": 3,
        "accepted": 0,
        "archived": 0,
        "not_found": 0,
    }
    (mismatch,) = data["mismatched"]
    assert mismatch["plan_row_index"] == 1 and mismatch["table_id"] == TABLE
    assert mismatch["fields"] == [{"field": "ct", "planned": 2, "actual": 22}]
    assert data["missing"] == [
        {"table_id": TABLE, "plan_row_index": 2, "reason": "no_matching_result"}
    ]
    assert data["extra"][0]["state"] == "pending"
    assert data["source"]["mode"] == "payload"
    assert data["source"]["lookup"] == "created_window"
    assert data["source"]["schemas"] == [
        {"schema_id": SCHEMA, "table_id": TABLE, "records": 2, "fields": ["ct"]},
        {"schema_id": OTHER_SCHEMA, "table_id": TABLE, "records": 1, "fields": ["ct"]},
    ]
    assert [batch["schema_id"] for batch in data["batches"]] == [SCHEMA, OTHER_SCHEMA]
    assert "plug" in data["next_step"]
    assert len(fake.writes()) == writes
    assert [call.params["schemaId"] for call in list_calls(fake)] == [SCHEMA, OTHER_SCHEMA]
    assert all("createdAt.lte" not in call.params for call in list_calls(fake))

    planned = await runtime.planner.plan(
        ENTRY_ID, "results", {"table_id": TABLE, "assay_results": [result(1)]}
    )
    await runtime.planner.apply(planned.id)
    fake.complete_upload()
    plan_report = await runtime.planner.verify_results(planned.id)
    assert set(data) - {"source"} == set(plan_report)
    assert set(data["counts"]) == set(plan_report["counts"])


async def test_payload_by_mixed_ids_pairs_client_ids_first(runtime, fake) -> None:
    # Benchling holds the two values swapped, so pairing by fields would match both.
    first, second = sdk_push(
        fake, [result(6, id=CLIENT_ONE), result(5, id=CLIENT_TWO)], table_id=TABLE
    )
    assert (first, second) == (CLIENT_ONE, CLIENT_TWO)
    v3_second = next(item["v3Id"] for item in fake.assay_results if item["id"] == second)
    body = {"assayResults": [result(5, id=CLIENT_ONE), result(6, id=CLIENT_TWO)]}

    data = await runtime.planner.verify_payload(body, result_ids=[first, v3_second])

    assert data["counts"]["matched"] == 0 and data["counts"]["mismatched"] == 2
    assert [(row["plan_row_index"], row["v2_id"]) for row in data["mismatched"]] == [
        (0, first),
        (1, second),
    ]
    assert data["source"]["lookup"] == "ids" and data["source"]["requested"] == 2


async def test_payload_by_ids_reports_not_found_and_records_of_other_schemas(runtime, fake) -> None:
    (created,) = sdk_push(fake, [result(1)])
    (other,) = sdk_push(fake, [result(7, OTHER_SCHEMA)])
    missing_id = "33333333-3333-4333-8333-333333333333"
    body = [result(1), result(2)]

    data = await runtime.planner.verify_payload(body, result_ids=[created, other, missing_id])

    assert data["counts"]["matched"] == 1 and data["counts"]["not_found"] == 1
    assert data["missing"] == [
        {"table_id": None, "plan_row_index": 1, "reason": "result_not_found"}
    ]
    assert data["extra"][0]["v2_id"] == other
    assert data["batches"][-1] == {
        "table_id": None,
        "planned": 0,
        "fetched": 1,
        "matched": 0,
        "mismatched": 0,
        "missing": 0,
        "extra": 1,
        "schema_id": OTHER_SCHEMA,
    }
    assert data["source"]["not_found"] == [missing_id]


async def test_entry_drops_accepted_records_of_other_entries_but_keeps_pending(
    runtime, fake
) -> None:
    accepted, pending, elsewhere = sdk_push(fake, [result(1), result(2), result(1)])
    fake.accept_results([accepted])
    fake.accept_results([elsewhere], entry_id="etr_other")

    data = await runtime.planner.verify_payload(
        [result(1), result(2)], created_after=AFTER, entry=ENTRY_ID
    )

    assert data["ok"] is True
    assert data["counts"]["accepted"] == 1 and data["counts"]["pending"] == 1
    assert data["source"]["other_entry_dropped"] == 1 and data["source"]["entry_id"] == ENTRY_ID
    assert data["entry_id"] == ENTRY_ID and data["web_url"] == entry_web_url(ENTRY_ID)
    assert [row["v2_id"] for row in data["needs_accept"]] == [pending]
    assert f"entry {ENTRY_ID} at {entry_web_url(ENTRY_ID)}" in data["next_step"]


async def test_created_before_is_sent_and_a_page_cap_is_reported(
    runtime, fake, monkeypatch
) -> None:
    sdk_push(fake, [result(1), result(2)])
    fake.assay_result_page_size = 1
    monkeypatch.setattr(planner_module, "_PAYLOAD_PAGES", 1)

    data = await runtime.planner.verify_payload(
        [result(1), result(2)], created_after=AFTER, created_before="2026-11-20T00:00:00Z"
    )

    (call,) = list_calls(fake)
    assert call.params["createdAt.lte"] == "2026-11-20T00:00:00Z"
    assert data["source"]["created_before"] == "2026-11-20T00:00:00Z"
    assert data["counts"]["matched"] == 1 and data["counts"]["missing"] == 1
    assert "narrow it with created_before" in data["source"]["warnings"][0]


async def test_both_modes_use_diff_batch_and_the_shared_report(runtime, fake, monkeypatch) -> None:
    diffs, reports = [], []
    diff_batch, build = readback.diff_batch, ChangePlanner._verify_report

    def spy_diff(batch, fetched, **kwargs):
        diffs.append(batch.get("schema_id"))
        return diff_batch(batch, fetched, **kwargs)

    def spy_report(units, **kwargs):
        reports.append(kwargs["head"].get("source", {}).get("mode", "plan"))
        return build(units, **kwargs)

    monkeypatch.setattr(readback, "diff_batch", spy_diff)
    monkeypatch.setattr(ChangePlanner, "_verify_report", staticmethod(spy_report))
    planned = await runtime.planner.plan(
        ENTRY_ID, "results", {"table_id": TABLE, "assay_results": [result(1)]}
    )
    await runtime.planner.apply(planned.id)
    fake.complete_upload()

    await runtime.planner.verify_results(planned.id)
    await runtime.planner.verify_payload(
        [result(1, id=fake.assay_results[0]["id"]), result(2)], created_after=AFTER
    )

    assert reports == ["plan", "payload"]
    assert len(diffs) == 3


@pytest.mark.parametrize(
    ("payload", "message"),
    [
        ("text", "bulk-create body"),
        ({"results": []}, "needs an assayResults list"),
        ({"assayResults": []}, "no results"),
        ([], "no results"),
        ({"assayResults": [{"fields": {}}]}, "missing schemaId"),
        ({"assayResults": [{"schemaId": SCHEMA, "fields": []}]}, "fields must be an object"),
        ({"assayResults": ["row"]}, "must be an object"),
        ({"assayResults": [result(1, id=5)]}, "id must be a string"),
        ({"tableId": 5, "assayResults": [result(1)]}, "tableId"),
        ({"assayResults": [result(1)] * 4001}, "at most 4000"),
        ([{"assayResults": [result(1)]}, result(2)], "only result objects or only"),
    ],
)
async def test_bad_payload_shapes_are_validation_errors(runtime, fake, payload, message) -> None:
    with pytest.raises(ValidationError, match=message):
        await runtime.planner.verify_payload(payload, created_after=AFTER)
    assert fake.calls == []


@pytest.mark.parametrize(
    ("arguments", "message"),
    [
        ({}, "exactly one of result_ids or created_after"),
        ({"result_ids": ["x"], "created_after": AFTER}, "exactly one"),
        ({"result_ids": ["x"], "created_before": AFTER}, "created_before applies only"),
        ({"created_after": AFTER, "entry": " "}, "entry must be"),
        ({"created_after": AFTER, "sample": 0}, "sample must be"),
        ({"created_after": AFTER, "created_before": "soon"}, "created_before must be"),
        ({"created_after": AFTER, "created_before": 5}, "created_before must be"),
    ],
)
async def test_bad_lookup_arguments_are_validation_errors(runtime, fake, arguments, message):
    with pytest.raises(ValidationError, match=message):
        await runtime.planner.verify_payload([result(1)], **arguments)


def test_payload_batches_group_by_schema_with_payload_row_indexes() -> None:
    batches = readback.payload_batches(
        [
            {"tableId": TABLE, "assayResults": [result(1), result(2, OTHER_SCHEMA)]},
            {"tableId": "strtbl_second", "assayResults": [result(3, id=CLIENT_ONE)]},
        ]
    )

    assert [(b["schema_id"], b["table_id"], b["row_indexes"]) for b in batches] == [
        (SCHEMA, None, [0, 2]),
        (OTHER_SCHEMA, TABLE, [1]),
    ]
    assert batches[0]["records"][1] == result(3, id=CLIENT_ONE)
    assert readback.payload_batches([result(1)])[0]["table_id"] is None


@pytest.fixture
def invoke(benchling_env, fake, monkeypatch):
    monkeypatch.setattr(cli, "Runtime", lambda environment: runtime_for(fake, environment))

    def run(*args, stdin=None):
        response = CliRunner().invoke(cli.app, [*args, "--env", "sandbox"], input=stdin)
        return response.exit_code, json.loads(response.stdout)

    return run


def test_cli_verifies_a_payload_file_and_stdin(invoke, fake, tmp_path) -> None:
    sdk_push(fake, [result(1)])
    body = {"tableId": TABLE, "assayResults": [result(1)]}
    path = tmp_path / "body.json"
    path.write_text(json.dumps(body))

    code, printed = invoke("results", "verify", "--payload", str(path), "--created-after", AFTER)
    stdin_code, from_stdin = invoke(
        "results", "verify", "--payload", "-", "--created-after", AFTER, stdin=json.dumps(body)
    )

    assert code == stdin_code == 0
    assert printed["data"]["ok"] is True and printed["data"]["counts"]["matched"] == 1
    assert from_stdin["data"] == printed["data"]


@pytest.mark.parametrize(
    "args",
    [
        ("results", "verify"),
        ("results", "verify", "0" * 32, "--payload", "body.json"),
        ("results", "verify", "0" * 32, "--id", "x"),
        ("results", "verify", "0" * 32, "--created-after", AFTER),
    ],
)
def test_cli_needs_exactly_one_of_plan_or_payload(invoke, fake, args) -> None:
    code, printed = invoke(*args)
    assert code != 0
    assert printed["error"]["code"] == "validation_error"
    assert fake.calls == []


def test_cli_unreadable_payload_does_not_echo_its_content(invoke, tmp_path) -> None:
    path = tmp_path / "body.json"
    path.write_text('{"assayResults": [secret-looking-value')

    code, printed = invoke("results", "verify", "--payload", str(path), "--created-after", AFTER)

    assert code != 0 and printed["error"]["code"] == "validation_error"
    assert printed["error"]["message"] == "Could not read --payload"
    assert "secret-looking-value" not in json.dumps(printed)
