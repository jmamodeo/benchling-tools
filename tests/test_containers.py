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


def _container_lists(fake) -> list:
    return [call for call in fake.calls if call.path == "/api/v2/containers"]


async def test_find_containers_many_barcodes_reports_each_outcome(runtime, fake) -> None:
    fake.invalid_barcodes = {"BAR-BAD"}
    found = await runtime.service.find_containers(
        barcode=["BAR-BATCH", "BAR-BAD", "BAR-SOURCE", "BAR-NONE", "BAR-DEST", "BAR-SOURCE"]
    )
    assert found == {
        "requested": 5,
        "found": [
            {
                "barcode": "BAR-BATCH",
                "container_id": "con_batch1",
                "schema_id": "contsch_abc123",
                "contents": [
                    {
                        "entity_id": "ent_sample1",
                        "entity_registry_id": None,
                        "batch_id": "bat_sample1",
                    },
                    {
                        "entity_id": "ent_registered1",
                        "entity_registry_id": "REG-0001",
                        "batch_id": None,
                    },
                ],
                "archived": False,
            },
            {
                "barcode": "BAR-SOURCE",
                "container_id": "con_source1",
                "schema_id": "contsch_abc123",
                "contents": [
                    {"entity_id": "ent_sample1", "entity_registry_id": None, "batch_id": None}
                ],
                "archived": False,
            },
            {
                "barcode": "BAR-DEST",
                "container_id": "con_dest1",
                "schema_id": "contsch_abc123",
                "contents": [],
                "archived": False,
            },
        ],
        "invalid_barcodes": ["BAR-BAD"],
        "missing_barcodes": ["BAR-NONE"],
        "empty_containers": ["BAR-DEST"],
    }
    requests = _container_lists(fake)
    assert [call.params["barcodes"] for call in requests] == [
        "BAR-BATCH,BAR-BAD,BAR-SOURCE,BAR-NONE,BAR-DEST",
        "BAR-BATCH,BAR-SOURCE,BAR-NONE,BAR-DEST",
    ]
    assert {call.params["pageSize"] for call in requests} == {"100"}


async def test_find_containers_chunks_barcodes_at_100_and_follows_pages(runtime, fake) -> None:
    barcodes = [f"BAR-{index:03d}" for index in range(150)]
    found = await runtime.service.find_containers(barcode=barcodes)
    assert found["requested"] == 150
    assert found["missing_barcodes"] == barcodes
    requests = _container_lists(fake)
    assert [len(call.params["barcodes"].split(",")) for call in requests] == [100, 50]

    fake.container_page_size = 2
    fake.calls.clear()
    paged = await runtime.service.find_containers(barcode=["BAR-SOURCE", "BAR-DEST", "BAR-BATCH"])
    assert [item["container_id"] for item in paged["found"]] == [
        "con_source1",
        "con_dest1",
        "con_batch1",
    ]
    assert [call.params.get("nextToken") for call in _container_lists(fake)] == [None, "2"]


@pytest.mark.parametrize(("count", "chunks"), [(100, [100]), (101, [100, 1])])
async def test_find_containers_chunk_boundary(runtime, fake, count, chunks) -> None:
    barcodes = [f"BAR-{index:03d}" for index in range(count)]
    found = await runtime.service.find_containers(barcode=barcodes)
    assert found["requested"] == count
    assert found["missing_barcodes"] == barcodes
    requests = _container_lists(fake)
    assert [len(call.params["barcodes"].split(",")) for call in requests] == chunks
    sent = [item for call in requests for item in call.params["barcodes"].split(",")]
    assert sent == barcodes


async def test_find_containers_all_invalid_chunk_is_not_retried(runtime, fake) -> None:
    fake.invalid_barcodes = {"BAR-BAD1", "BAR-BAD2"}
    found = await runtime.service.find_containers(barcode=["BAR-BAD1", "BAR-BAD2"])
    assert found["invalid_barcodes"] == ["BAR-BAD1", "BAR-BAD2"]
    assert found["found"] == found["missing_barcodes"] == []
    assert len(_container_lists(fake)) == 1


async def test_find_containers_other_400_is_raised(runtime, fake) -> None:
    fake.fail("GET", "/api/v2/containers", 400)
    with pytest.raises(ValidationError, match="Benchling rejected") as raised:
        await runtime.service.find_containers(barcode=["BAR-SOURCE", "BAR-DEST"])
    assert "invalid_barcodes" not in raised.value.details
    assert len(_container_lists(fake)) == 1


async def test_find_containers_single_invalid_barcode_names_it(runtime, fake) -> None:
    fake.invalid_barcodes = {"BAR-BAD"}
    with pytest.raises(ValidationError) as raised:
        await runtime.service.find_containers(barcode="BAR-BAD")
    assert raised.value.details["invalid_barcodes"] == ["BAR-BAD"]
    assert raised.value.details["hint"] == "Benchling did not recognise these barcodes: BAR-BAD"
    assert len(_container_lists(fake)) == 1


async def test_find_containers_barcode_input_is_checked(runtime) -> None:
    with pytest.raises(ValidationError, match="repeat --barcode"):
        await runtime.service.find_containers(barcode="BAR-SOURCE,BAR-DEST")
    with pytest.raises(ValidationError, match="repeat --barcode"):
        await runtime.service.find_containers(barcode=["BAR-SOURCE", "BAR-A,BAR-B"])
    with pytest.raises(ValidationError, match="non-empty"):
        await runtime.service.find_containers(barcode=[])
    with pytest.raises(ValidationError, match="non-empty"):
        await runtime.service.find_containers(barcode=["BAR-SOURCE", " "])
    with pytest.raises(ValidationError, match="cursor"):
        await runtime.service.find_containers(barcode=["BAR-SOURCE", "BAR-DEST"], cursor="50")
    single = await runtime.service.find_containers(barcode=["BAR-SOURCE", "BAR-SOURCE"])
    assert [item["id"] for item in single["items"]] == ["con_source1"]


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
