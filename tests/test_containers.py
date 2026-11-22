from __future__ import annotations

import pytest

from benchling_tools.errors import ConflictError, ValidationError
from benchling_tools.models import PlanStatus


async def test_container_read_and_find_operations(runtime, fake) -> None:
    container = await runtime.service.get_container("con_source1")
    assert container["name"] == "Source vial"

    by_barcode = await runtime.service.find_containers(barcode="BAR-SOURCE", limit=1)
    assert [item["id"] for item in by_barcode["items"]] == ["con_source1"]
    assert by_barcode["next_token"] is None

    by_name = await runtime.service.find_containers(name="Destination vial")
    assert [item["id"] for item in by_name["items"]] == ["con_dest1"]

    schemas = await runtime.service.list_container_schemas()
    assert schemas["items"][0]["id"] == "contsch_abc123"
    assert schemas["items"][0]["fieldDefinitions"][0]["name"] == "Storage note"

    with pytest.raises(ValidationError, match="exactly one"):
        await runtime.service.find_containers()
    with pytest.raises(ValidationError, match="exactly one"):
        await runtime.service.find_containers(barcode="BAR-SOURCE", name="Source vial")
    assert not any(call.method != "GET" for call in fake.calls)


async def test_container_create_is_preview_then_single_apply(runtime, fake) -> None:
    specification = {
        "operation": "create",
        "schema_id": "contsch_abc123",
        "name": "Synthetic new vial",
        "barcode": "BAR-NEW",
    }
    plan = await runtime.planner.plan(None, "containers", specification)
    assert plan.entry_id is None
    assert plan.before["schema"]["id"] == "contsch_abc123"
    assert not any(call.method == "POST" for call in fake.calls)

    applied = await runtime.planner.apply(plan.id)
    assert applied["journal"]["status"] == PlanStatus.SUCCEEDED.value
    assert fake.containers[-1]["barcode"] == "BAR-NEW"
    assert [call.path for call in fake.calls if call.method == "POST"] == ["/api/v2/containers"]

    with pytest.raises(ConflictError, match="already been applied"):
        await runtime.planner.apply(plan.id)
    assert len([call for call in fake.calls if call.method == "POST"]) == 1


async def test_container_transfer_rechecks_both_containers(runtime, fake) -> None:
    specification = {
        "operation": "transfer",
        "source_container_id": "con_source1",
        "destination_container_id": "con_dest1",
        "transfer_quantity": {"value": 1, "units": "mL"},
        "destination_contents": [{"entity_id": "ent_sample1"}],
    }
    stale_plan = await runtime.planner.plan(None, "containers", specification)
    fake.containers[1]["modifiedAt"] = "2026-02-01T00:00:00Z"
    with pytest.raises(ConflictError, match="Destination container changed"):
        await runtime.planner.apply(stale_plan.id)
    assert fake.container_transfers == []
    assert runtime.repository.load_journal(stale_plan.id).status == PlanStatus.FAILED

    plan = await runtime.planner.plan(None, "containers", specification)
    applied = await runtime.planner.apply(plan.id)
    assert applied["journal"]["status"] == PlanStatus.SUCCEEDED.value
    assert fake.container_transfers[-1]["destination_id"] == "con_dest1"
    assert fake.container_transfers[-1]["body"]["sourceContainerId"] == "con_source1"


def test_container_change_specifications_are_strict() -> None:
    from benchling_tools.change_spec import parse_change_specification

    parsed = parse_change_specification(
        {
            "operation": "create",
            "schema_id": "contsch_abc123",
            "name": "Synthetic vial",
        }
    )
    assert parsed == {
        "operation": "create",
        "schema_id": "contsch_abc123",
        "name": "Synthetic vial",
    }
    with pytest.raises(ValidationError, match="invalid"):
        parse_change_specification(
            {
                "operation": "create",
                "schema_id": "contsch_abc123",
                "unreviewed": True,
            }
        )
    with pytest.raises(ValidationError, match="quantity"):
        parse_change_specification(
            {
                "operation": "create",
                "schema_id": "contsch_abc123",
                "quantity": {"value": 1, "units": "mL"},
            }
        )
