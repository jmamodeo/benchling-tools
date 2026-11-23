"""The examples/ results fixtures run end to end against the offline fake API."""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
from conftest import runtime_for
from fake_benchling import ENTRY_ID
from test_contract_requests import _bulk_create_errors
from typer.testing import CliRunner

from benchling_tools import cli
from benchling_tools.change_spec import parse_change_specification

EXAMPLES = Path(__file__).resolve().parents[1] / "examples"
RESULT_FIXTURES = sorted(EXAMPLES.glob("results-*.json"))
# Synthetic ids only: a known prefix, an underscore, then lowercase letters and digits.
_ID_VALUE = re.compile(r"^(strtbl|assaysch)_[a-z0-9]+$")


@pytest.fixture
def invoke(benchling_env, fake, monkeypatch):
    fake.add_plate_reader_table()
    monkeypatch.setattr(cli, "Runtime", lambda environment: runtime_for(fake, environment))

    def run(*args: str):
        result = CliRunner().invoke(cli.app, [*args, "--env", "sandbox"])
        return result.exit_code, json.loads(result.stdout)

    return run


def _plan(invoke, name: str):
    return invoke(
        "changes",
        "plan",
        "--entry",
        ENTRY_ID,
        "--family",
        "results",
        "--spec",
        str(EXAMPLES / name),
    )


def test_result_fixtures_exist_and_parse_as_change_specifications() -> None:
    assert [path.name for path in RESULT_FIXTURES] == [
        "results-invalid.json",
        "results-multi-schema.json",
        "results-plate-reader.json",
    ]
    for path in RESULT_FIXTURES:
        parse_change_specification(json.loads(path.read_text(encoding="utf-8")))


def test_result_fixtures_use_only_synthetic_ids() -> None:
    for path in RESULT_FIXTURES:
        spec = json.loads(path.read_text(encoding="utf-8"))
        batches = spec.get("batches") or [spec]
        for batch in batches:
            assert _ID_VALUE.match(batch["table_id"]), path.name
            for record in batch["assay_results"]:
                assert _ID_VALUE.match(record["schemaId"]), path.name
                assert set(record) == {"schemaId", "fields"}, path.name


def test_plate_reader_fixture_previews_then_applies_one_bulk_create(invoke, fake) -> None:
    code, planned = _plan(invoke, "results-plate-reader.json")
    assert code == 0, planned
    assert fake.writes() == []
    assert [(t["table_id"], t["row_count"]) for t in planned["data"]["before"]["tables"]] == [
        ("strtbl_plate", 4)
    ]
    code, applied = invoke("changes", "apply", planned["data"]["id"])
    assert code == 0, applied
    assert [payload["tableId"] for payload in fake.bulk_payloads] == ["strtbl_plate"]
    payload = fake.bulk_payloads[0]
    assert [row["fields"]["well"]["value"] for row in payload["assayResults"]] == [
        "A1",
        "A2",
        "A3",
        "B1",
    ]
    assert _bulk_create_errors(payload) == []
    assert "Plate reader" in applied["data"]["next_step"]


def test_multi_schema_fixture_submits_one_batch_per_table(invoke, fake) -> None:
    code, planned = _plan(invoke, "results-multi-schema.json")
    assert code == 0, planned
    assert [
        (t["table_id"], t["schema_id"], t["row_count"]) for t in planned["data"]["before"]["tables"]
    ] == [("strtbl_plate", "assaysch_plate", 2), ("strtbl_example", "assaysch_abc123", 1)]
    fake.bulk_responses = [{"taskId": "task_plate"}, {"taskId": "task_example"}]
    code, applied = invoke("changes", "apply", planned["data"]["id"])
    assert code == 0, applied
    assert [payload["tableId"] for payload in fake.bulk_payloads] == [
        "strtbl_plate",
        "strtbl_example",
    ]
    assert all(_bulk_create_errors(payload) == [] for payload in fake.bulk_payloads)


def test_invalid_fixture_is_a_validation_error_and_writes_nothing(invoke, fake) -> None:
    code, refused = _plan(invoke, "results-invalid.json")
    assert code == 2
    assert refused["ok"] is False
    assert refused["error"]["code"] == "validation_error"
    problems = refused["error"]["details"]["row_problems"]
    assert [(p["row_index"], p["table_id"]) for p in problems] == [
        (0, "strtbl_plate"),
        (1, "strtbl_plate"),
    ]
    assert problems[0]["details"]["field"] == "absorbance"
    assert problems[1]["details"]["missing_required_fields"] == ["well"]
    assert fake.writes() == []
