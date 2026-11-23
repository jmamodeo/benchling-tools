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
