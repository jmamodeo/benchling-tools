from __future__ import annotations

import pytest
from conftest import runtime_for
from fake_benchling import ENTRY_ID, TEXT_PART

from benchling_tools.errors import (
    ConflictError,
    ValidationError,
)


async def delete_plan(runtime):
    return await runtime.planner.plan(
        ENTRY_ID, "document", {"operation": "delete", "from_part_id": TEXT_PART}
    )


async def test_unknown_family_lists_supported_families(runtime) -> None:
    with pytest.raises(ValidationError) as caught:
        await runtime.planner.plan(ENTRY_ID, "everything", {})
    assert caught.value.details["supported"] == [
        "entry",
        "document",
        "table",
        "fields",
        "results",
        "containers",
    ]


async def test_plan_is_persisted_with_stable_checksum_and_diff(runtime) -> None:
    planned = await delete_plan(runtime)
    loaded = runtime.repository.load_plan(planned.id)
    assert loaded.checksum == planned.checksum != ""
    assert loaded.diff
    assert loaded.env == "sandbox"
    assert loaded.tenant_url == "https://your-tenant-dev.benchling.com"
    assert runtime.repository.load_journal(planned.id).status.value == "pending"


async def test_plan_resolves_display_ids(runtime, fake) -> None:
    planned = await runtime.planner.plan(
        "EXP-1", "document", {"operation": "delete", "from_part_id": TEXT_PART}
    )
    assert planned.entry_id == ENTRY_ID


async def test_tampered_plan_is_rejected(runtime, fake) -> None:
    planned = await delete_plan(runtime)
    path = runtime.repository.config.plans_dir / f"{planned.id}.json"
    path.write_text(path.read_text().replace(TEXT_PART, "docpart_text2"))
    with pytest.raises(ValidationError):
        await runtime.planner.apply(planned.id)
    assert fake.writes() == []


@pytest.mark.parametrize("plan_id", ["../escape", "short", "g" * 32, ""])
async def test_malformed_plan_ids_are_rejected(runtime, plan_id) -> None:
    with pytest.raises(ValidationError):
        await runtime.planner.apply(plan_id)
    with pytest.raises(ValidationError):
        await runtime.planner.status(plan_id)


async def test_unknown_plan_id_is_rejected(runtime) -> None:
    with pytest.raises(ValidationError, match="Unknown change plan"):
        await runtime.planner.status("a" * 32)
    with pytest.raises(ValidationError, match="Unknown change plan"):
        await runtime.planner.apply("a" * 32)


async def test_plan_cannot_be_used_from_another_environment(runtime, fake, monkeypatch) -> None:
    from conftest import configure_env

    configure_env("other", "https://other-tenant.benchling.com", secret="other-secret-value")
    other = runtime_for(fake, "other")
    try:
        planned = await delete_plan(runtime)
        for call in (other.planner.apply, other.planner.status):
            with pytest.raises(ValidationError, match="different environment or tenant") as caught:
                await call(planned.id)
            assert caught.value.details == {
                "plan_env": "sandbox",
                "active_env": "other",
            }
        assert fake.writes() == []
        assert runtime.repository.load_journal(planned.id).status.value == "pending"
    finally:
        await other.close()


async def test_second_apply_is_refused_after_success(runtime, fake) -> None:
    planned = await delete_plan(runtime)
    await runtime.planner.apply(planned.id)
    with pytest.raises(ConflictError) as caught:
        await runtime.planner.apply(planned.id)
    assert caught.value.details["status"] == "succeeded"
    assert len(fake.writes()) == 1
