"""Results-table row capacity: the changes plan check and the results capacity report."""

from __future__ import annotations

import json

import pytest
from conftest import runtime_for
from fake_benchling import ENTRY_ID, TABLE_PART
from typer.testing import CliRunner

from benchling_tools import capacity, cli
from benchling_tools.config import ConfigStore
from benchling_tools.errors import NotFoundError, UpstreamError, ValidationError
from benchling_tools.plans import blobs
from benchling_tools.plans.planner import ChangePlanner

TABLE = "strtbl_example"
PART = "docpart_example"
PLOT = {
    "id": "assayfield_plot",
    "systemName": "plot",
    "name": "Plot",
    "__typename": "AttachmentLinkFieldDefinition",
    "derivationType": "DIRECT",
}
PLACEHOLDER = {
    "id": "strow_blank",
    "submissionStatus": "UNSUBMITTED",
    "isSubmittable": False,
    "cells": [{"inputValue": {}}],
}


def record(ct=12, **overrides):
    return {"schemaId": "assaysch_abc123", "fields": {"ct": {"value": ct}}, **overrides}


def spec(count: int) -> dict:
    return {"table_id": TABLE, "assay_results": [record(index) for index in range(count)]}


def only_table(report: dict) -> dict:
    [table] = [item for item in report["tables"] if item["table_id"] == TABLE]
    return table


def sidecars() -> list:
    directory = ConfigStore().uploads_dir
    return sorted(directory.iterdir()) if directory.exists() else []


async def applied_pending(runtime, fake, count: int) -> str:
    planned = await runtime.planner.plan(ENTRY_ID, "results", spec(count))
    await runtime.planner.apply(planned.id)
    fake.complete_upload()
    return planned.id


async def test_plan_over_the_row_limit_is_refused_before_any_file_is_staged(
    runtime, fake, tmp_path, monkeypatch
) -> None:
    fake.add_example_results_part(row_count=992)
    fake.field_defs["assaysch_abc123"].append(dict(PLOT))
    path = tmp_path / "plot.csv"
    path.write_text("well,ct\nA1,12\n")
    staged: list = []
    monkeypatch.setattr(
        blobs.FileStager, "stage", lambda self, *args, **kwargs: staged.append(args)
    )
    records = [record(index, fields={"ct": {"value": index}}) for index in range(9)]
    records[0]["fields"]["plot"] = {"value": {"file": str(path)}}
    with pytest.raises(ValidationError) as raised:
        await runtime.planner.plan(
            ENTRY_ID, "results", {"table_id": TABLE, "assay_results": records}
        )
    error = raised.value
    assert "1,000-row table limit" in error.message
    assert error.details["reason"] == "table_capacity_exceeded"
    assert error.details["limit"] == 1000
    [table] = error.details["tables"]
    assert table["table_id"] == TABLE
    assert table["part_id"] == PART
    assert table["row_count"] == 992
    assert table["used_rows"] == 992
    assert table["planned"] == 9
    assert table["remaining_without_pending"] == -1
    assert table["fits"] is False
    assert "new entry or another results table" in error.details["hint"]
    assert staged == []
    assert sidecars() == []
    assert fake.writes() == []
    assert list(runtime.repository.config.plans_dir.glob("*.json")) == []


async def test_plan_that_fits_is_saved_without_a_capacity_warning(runtime, fake) -> None:
    fake.add_example_results_part(row_count=992)
    planned = await runtime.planner.plan(ENTRY_ID, "results", spec(8))
    assert not any("1,000-row" in warning for warning in planned.warnings)
    assert not any("row count could not be read" in warning for warning in planned.warnings)


async def test_plan_that_fits_only_without_pending_results_warns(runtime, fake) -> None:
    fake.add_example_results_part(row_count=990)
    await applied_pending(runtime, fake, 5)
    planned = await runtime.planner.plan(ENTRY_ID, "results", spec(8))
    [warning] = [item for item in planned.warnings if "pending results count" in item]
    assert "does not document" in warning
    assert TABLE in warning


async def test_plan_for_a_table_without_a_v3_part_warns_that_capacity_was_not_checked(
    runtime,
) -> None:
    planned = await runtime.planner.plan(ENTRY_ID, "results", spec(1))
    assert any("Its capacity was not checked" in warning for warning in planned.warnings)


async def test_the_toolkit_batch_message_does_not_call_500_a_benchling_limit(runtime) -> None:
    with pytest.raises(ValidationError) as raised:
        await runtime.planner.plan(ENTRY_ID, "results", spec(501))
    assert raised.value.message == capacity.TOOLKIT_BATCH_MESSAGE
    assert "This toolkit uploads at most 500" in raised.value.message
    assert "4,000 results per bulk-create request" in raised.value.message
    assert "Benchling accepts at most 500" not in raised.value.message
    assert raised.value.details == {"table_id": TABLE, "records": 501, "limit": 500}


async def test_500_records_per_table_still_plan(runtime, fake) -> None:
    fake.add_example_results_part(rows=[])
    planned = await runtime.planner.plan(ENTRY_ID, "results", spec(500))
    assert len(planned.after["records"]) == 500


async def test_capacity_reports_current_usage_with_v3_row_count(runtime, fake) -> None:
    fake.add_example_results_part()
    data = await runtime.planner.results_capacity(ENTRY_ID)
    assert data["source"] == {"mode": "current"}
    assert data["limit"] == 1000
    assert data["fits"] is True
    assert data["next_step"] is None
    assert data["resolved"]["entry_id"] == ENTRY_ID
    assert only_table(data) == {
        "table_id": TABLE,
        "part_id": PART,
        "name": "Assay results",
        "schema_id": "assaysch_abc123",
        "row_count": 3,
        "submission_status": "SUBMITTED",
        "placeholder_rows": 0,
        "used_rows": 3,
        "pending_known": 0,
        "planned": 0,
        "limit": 1000,
        "remaining_without_pending": 997,
        "remaining_with_pending": 997,
        "fits": True,
    }
    assert fake.writes() == []


async def test_an_empty_placeholder_row_counts_as_zero_used(runtime, fake) -> None:
    fake.add_example_results_part(rows=[dict(PLACEHOLDER)], submission_status="UNSUBMITTED")
    table = only_table(await runtime.planner.results_capacity(ENTRY_ID))
    assert (table["row_count"], table["placeholder_rows"], table["used_rows"]) == (1, 1, 0)
    assert table["remaining_with_pending"] == 1000
    rows = [call for call in fake.calls if call.path.endswith("/rows/items")]
    assert [call.params for call in rows] == [{"pageSize": "1"}]


async def test_one_real_row_is_not_a_placeholder(runtime, fake) -> None:
    real = {**PLACEHOLDER, "cells": [{"inputValue": {"value": "A1"}}]}
    fake.add_example_results_part(rows=[real])
    table = only_table(await runtime.planner.results_capacity(ENTRY_ID))
    assert (table["row_count"], table["placeholder_rows"], table["used_rows"]) == (1, 0, 1)


async def test_rows_are_not_read_when_the_row_count_is_above_one(runtime, fake) -> None:
    fake.add_example_results_part(row_count=992)
    await runtime.planner.results_capacity(ENTRY_ID)
    assert not any(call.path.endswith("/rows/items") for call in fake.calls)


@pytest.mark.parametrize(
    "row",
    [
        {**PLACEHOLDER, "isArchived": True},
        {**PLACEHOLDER, "result": {"id": "result_1"}},
        {**PLACEHOLDER, "submissionStatus": "SUBMITTED"},
        {**PLACEHOLDER, "cells": None},
        {**PLACEHOLDER, "cells": ["bad"]},
        "bad",
    ],
)
def test_only_an_empty_unsubmitted_row_without_a_result_is_a_placeholder(row) -> None:
    assert capacity.is_placeholder_row(row) is False
    assert capacity.is_placeholder_row(PLACEHOLDER) is True


async def test_capacity_counts_this_homes_pending_records_per_table(runtime, fake) -> None:
    fake.add_example_results_part(row_count=990)
    await applied_pending(runtime, fake, 5)
    data = await runtime.planner.results_capacity(ENTRY_ID, spec=spec(8))
    table = only_table(data)
    assert table["pending_known"] == 5
    assert table["planned"] == 8
    assert table["remaining_without_pending"] == 2
    assert table["remaining_with_pending"] == -3
    assert table["fits"] == data["fits"] == "unknown_pending"
    assert "pending results do not count" in data["next_step"]
    assert any("does not document" in warning for warning in data["warnings"])


async def test_accepted_records_are_not_counted_as_pending(runtime, fake) -> None:
    fake.add_example_results_part(row_count=10)
    await runtime.planner.apply((await runtime.planner.plan(ENTRY_ID, "results", spec(2))).id)
    fake.accept_results(fake.complete_upload())
    table = only_table(await runtime.planner.results_capacity(ENTRY_ID))
    assert table["pending_known"] == 0


async def test_capacity_over_the_limit_is_a_report_that_names_the_table(runtime, fake) -> None:
    fake.add_example_results_part(row_count=992)
    data = await runtime.planner.results_capacity(ENTRY_ID, spec=spec(9))
    assert data["fits"] is False
    assert '"Assay results" (strtbl_example)' in data["next_step"]
    assert "new entry or another results table" in data["next_step"]


async def test_capacity_lists_every_results_table_and_unmatched_ones_as_unknown(
    runtime, fake
) -> None:
    fake.add_example_results_part()
    fake.add_plate_reader_table()
    data = await runtime.planner.results_capacity(ENTRY_ID)
    plate = next(item for item in data["tables"] if item["table_id"] == "strtbl_plate")
    assert plate["part_id"] is None
    assert plate["row_count"] is None
    assert plate["fits"] == "unknown"
    assert plate["remaining_with_pending"] is None
    assert data["fits"] == "unknown"
    assert any("strtbl_plate" in warning for warning in data["warnings"])


async def test_plan_and_spec_and_bundle_give_the_numbers_changes_plan_checks(
    runtime, fake, tmp_path, monkeypatch
) -> None:
    fake.add_example_results_part(row_count=900)
    seen: list = []
    shared = ChangePlanner.results_table_capacity

    async def spy(self, entry_id, planned, *, all_tables=False):
        seen.append(dict(planned))
        return await shared(self, entry_id, planned, all_tables=all_tables)

    monkeypatch.setattr(ChangePlanner, "results_table_capacity", spy)
    planned = await runtime.planner.plan(ENTRY_ID, "results", spec(7))
    bundle = tmp_path / "bundle.json"
    runtime.repository.export_plan_bundle(planned.id, str(bundle), force=False)
    by_plan = await runtime.planner.results_capacity(ENTRY_ID, plan_id=planned.id)
    by_bundle = await runtime.planner.results_capacity(ENTRY_ID, plan_id=str(bundle))
    by_spec = await runtime.planner.results_capacity(ENTRY_ID, spec=spec(7))
    assert seen == [{TABLE: 7}] * 4
    assert by_plan["source"] == {"mode": "plan", "plan_id": planned.id, "status": "pending"}
    assert by_spec["source"] == {"mode": "spec"}
    assert only_table(by_plan) == only_table(by_bundle) == only_table(by_spec)
    assert only_table(by_spec)["remaining_without_pending"] == 93


async def test_an_applied_plan_adds_no_planned_rows(runtime, fake) -> None:
    fake.add_example_results_part(row_count=10)
    plan_id = await applied_pending(runtime, fake, 3)
    data = await runtime.planner.results_capacity(ENTRY_ID, plan_id=plan_id)
    table = only_table(data)
    assert (table["planned"], table["pending_known"]) == (0, 3)
    assert any("already applied" in warning for warning in data["warnings"])


async def test_capacity_refuses_a_plan_for_another_family_or_entry(runtime, fake) -> None:
    table_plan = await runtime.planner.plan(
        ENTRY_ID,
        "table",
        {"operation": "append_rows", "table_part_id": TABLE_PART, "rows": [["D", "4"]]},
    )
    with pytest.raises(ValidationError, match="Only result upload plans"):
        await runtime.planner.results_capacity(ENTRY_ID, plan_id=table_plan.id)
    results_plan = await runtime.planner.plan(ENTRY_ID, "results", spec(1))
    resolved = await runtime.service.resolve_entry(ENTRY_ID)
    original = runtime.service.resolve_entry

    async def other_entry(reference):
        if reference == "etr_other":
            return {**resolved, "entry_id": "etr_other"}
        return await original(reference)

    runtime.service.resolve_entry = other_entry
    with pytest.raises(ValidationError, match="another entry"):
        await runtime.planner.results_capacity("etr_other", plan_id=results_plan.id)


@pytest.mark.parametrize(
    ("arguments", "message"),
    [
        ({"plan_id": "plan_x", "spec": spec(1)}, "only one of"),
        ({"include_schema_pending": True}, "go together"),
        ({"created_after": "2026-01-01T00:00:00Z"}, "go together"),
        ({"spec": {"operation": "update", "csv_path": "x.csv"}}, "does not add rows"),
        ({"spec": spec(501)}, "This toolkit uploads at most 500"),
    ],
)
async def test_capacity_rejects_invalid_arguments(runtime, arguments, message) -> None:
    with pytest.raises(ValidationError, match=message):
        await runtime.planner.results_capacity(ENTRY_ID, **arguments)


async def test_capacity_for_a_table_not_on_the_entry_is_not_found(runtime, fake) -> None:
    with pytest.raises(NotFoundError, match="not found on the selected entry"):
        await runtime.planner.results_capacity(
            ENTRY_ID, spec={"table_id": "strtbl_missing", "assay_results": [record()]}
        )


async def test_an_invalid_row_count_is_an_upstream_error(runtime, fake) -> None:
    fake.add_example_results_part(row_count=-1)
    with pytest.raises(UpstreamError, match="invalid row count"):
        await runtime.planner.results_capacity(ENTRY_ID)


async def test_schema_pending_is_reported_and_not_used_for_fits(runtime, fake) -> None:
    fake.add_example_results_part(row_count=995)
    fake.add_plate_reader_table()
    await applied_pending(runtime, fake, 2)
    created_after = min(item["createdAt"] for item in fake.assay_results)
    fake.assay_result_page_size = 1
    writes = len(fake.writes())
    data = await runtime.planner.results_capacity(
        ENTRY_ID, include_schema_pending=True, created_after=created_after
    )
    table = only_table(data)
    assert table["pending_schema"] == 2
    assert table["pending_schema_complete"] is True
    assert table["remaining_with_schema_pending"] == 3
    assert table["fits"] is True
    plate = next(item for item in data["tables"] if item["table_id"] == "strtbl_plate")
    assert plate["pending_schema"] == 0
    assert plate["remaining_with_schema_pending"] is None
    assert any("no entry filter" in warning for warning in data["warnings"])
    assert len(fake.writes()) == writes


async def test_schema_pending_stops_after_the_page_cap(runtime, fake, monkeypatch) -> None:
    from benchling_tools.plans import planner as planner_module

    monkeypatch.setattr(planner_module, "_SCHEMA_PENDING_PAGES", 1)
    fake.add_example_results_part(row_count=1)
    fake.results_parts[PART]["rows"] = [record()]
    await applied_pending(runtime, fake, 2)
    fake.assay_result_page_size = 1
    created_after = min(item["createdAt"] for item in fake.assay_results)
    table = only_table(
        await runtime.planner.results_capacity(
            ENTRY_ID, include_schema_pending=True, created_after=created_after
        )
    )
    assert table["pending_schema"] == 1
    assert table["pending_schema_complete"] is False


async def test_a_table_without_a_schema_has_no_schema_pending(runtime, fake) -> None:
    fake.result_table_schema = None
    data = await runtime.planner.results_capacity(
        ENTRY_ID, include_schema_pending=True, created_after="2026-01-01T00:00:00Z"
    )
    assert only_table(data)["pending_schema"] is None


def test_cli_capacity_reads_a_spec_file_and_exits_zero_when_it_does_not_fit(
    benchling_env, fake, monkeypatch, tmp_path
) -> None:
    fake.add_example_results_part(row_count=992)
    monkeypatch.setattr(cli, "Runtime", lambda environment: runtime_for(fake, environment))
    path = tmp_path / "spec.json"
    path.write_text(json.dumps(spec(9)))
    result = CliRunner().invoke(
        cli.app, ["results", "capacity", ENTRY_ID, "--spec", str(path), "--env", "sandbox"]
    )
    assert result.exit_code == 0, result.stdout
    data = json.loads(result.stdout)["data"]
    assert data["fits"] is False
    assert only_table(data)["remaining_without_pending"] == -1
    assert fake.writes() == []
