"""entry inspect and section get skip the linked-results walk they never return."""

from __future__ import annotations

from fake_benchling import ENTRY_ID


def _paths(fake, suffix: str) -> list:
    return [call for call in fake.calls if call.path.endswith(suffix)]


async def test_inspect_reads_no_linked_results(runtime, fake) -> None:
    inspected = await runtime.service.inspect_entry(ENTRY_ID)
    assert inspected["entry"]["id"] == ENTRY_ID
    assert _paths(fake, "/result/items") == []


async def test_section_get_reads_no_linked_results(runtime, fake) -> None:
    inspected = await runtime.service.inspect_entry(ENTRY_ID)
    section = inspected["sections"][0]["part_id"]
    fake.calls.clear()
    await runtime.service.get_section(ENTRY_ID, section)
    assert _paths(fake, "/result/items") == []


async def test_full_snapshot_still_reads_results_and_resolves_the_entry_once(runtime, fake) -> None:
    snapshot = await runtime.service.get_entry(ENTRY_ID, full=True)
    assert snapshot["result_retrieval"]["complete"] is True
    result_calls = _paths(fake, "/result/items")
    assert result_calls and all(c.params.get("entry.eq") == ENTRY_ID for c in result_calls)
    with_results = len(_paths(fake, f"/entry/{ENTRY_ID}"))
    fake.calls.clear()
    await runtime.service.get_entry(ENTRY_ID, full=True, include_results=False)
    # The results walk adds no entry reads of its own, however many pages it reads.
    assert len(_paths(fake, f"/entry/{ENTRY_ID}")) == with_results


def _fail_results(fake) -> None:
    fake.fail("GET", "/api/v3/result/items", 403, times=50)


def _reasons(data) -> list[str]:
    return [issue["reason"] for issue in data["issues"]]


async def test_inspect_completeness_ignores_a_failed_linked_results_walk(runtime, fake) -> None:
    baseline = await runtime.service.inspect_entry(ENTRY_ID)
    _fail_results(fake)
    inspected = await runtime.service.inspect_entry(ENTRY_ID)
    assert (inspected["completeness"], inspected["issues"]) == (
        baseline["completeness"],
        baseline["issues"],
    )
    assert "linked_results_unavailable" not in _reasons(inspected)


async def test_inspect_is_complete_when_only_linked_results_fail(runtime, fake) -> None:
    fake.parts = [part for part in fake.parts if part["id"] != "docpart_struct1"]
    _fail_results(fake)
    inspected = await runtime.service.inspect_entry(ENTRY_ID)
    assert "linked_results_unavailable" not in _reasons(inspected)
    full = await runtime.service.get_entry(ENTRY_ID, full=True)
    assert full["completeness"] == "partial"
    assert _reasons(full) == ["linked_results_unavailable"]


async def test_section_get_has_no_linked_results_issue(runtime, fake) -> None:
    section = (await runtime.service.inspect_entry(ENTRY_ID))["sections"][0]["part_id"]
    _fail_results(fake)
    got = await runtime.service.get_section(ENTRY_ID, section)
    assert "issues" not in got and "completeness" not in got
    assert _paths(fake, "/result/items") == []


async def test_full_entry_get_is_partial_when_linked_results_fail(runtime, fake) -> None:
    baseline = await runtime.service.get_entry(ENTRY_ID, full=True)
    assert "linked_results_unavailable" not in _reasons(baseline)
    _fail_results(fake)
    snapshot = await runtime.service.get_entry(ENTRY_ID, full=True)
    assert snapshot["completeness"] == "partial"
    assert _reasons(snapshot) == [*_reasons(baseline), "linked_results_unavailable"]
    assert snapshot["result_retrieval"]["complete"] is False
    assert snapshot["results"] == []


def test_cli_entry_get_full_partial_and_inspect_complete_on_failed_walk(
    benchling_env, fake, monkeypatch
) -> None:
    import json

    from conftest import runtime_for
    from typer.testing import CliRunner

    from benchling_tools import cli

    monkeypatch.setattr(cli, "Runtime", lambda environment: runtime_for(fake, environment))
    _fail_results(fake)
    runner = CliRunner()
    full = json.loads(
        runner.invoke(cli.app, ["entry", "get", ENTRY_ID, "--full", "--env", "sandbox"]).stdout
    )["data"]
    assert full["completeness"] == "partial"
    assert "linked_results_unavailable" in {i["reason"] for i in full["issues"]}
    inspected = json.loads(
        runner.invoke(cli.app, ["entry", "inspect", ENTRY_ID, "--env", "sandbox"]).stdout
    )["data"]
    assert "linked_results_unavailable" not in _reasons(inspected)
