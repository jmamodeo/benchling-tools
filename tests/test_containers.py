from __future__ import annotations

import pytest

from benchling_tools.errors import ConflictError, UpstreamError, ValidationError
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
                "parent_storage_id": None,
            },
            {
                "barcode": "BAR-SOURCE",
                "container_id": "con_source1",
                "schema_id": "contsch_abc123",
                "contents": [
                    {"entity_id": "ent_sample1", "entity_registry_id": None, "batch_id": None}
                ],
                "archived": False,
                "parent_storage_id": None,
            },
            {
                "barcode": "BAR-DEST",
                "container_id": "con_dest1",
                "schema_id": "contsch_abc123",
                "contents": [],
                "archived": False,
                "parent_storage_id": None,
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


async def test_find_containers_by_registry_id_resolves_and_reports_unregistered(
    runtime, fake
) -> None:
    fake.add_stored_containers()
    found = await runtime.service.find_containers(
        registry_ids=["REG-0001", "REG-9999"], registry="src_registry1"
    )
    assert found["registry"] == "src_registry1"
    assert found["entities"] == [
        {
            "registry_id": "REG-0001",
            "entity_id": "ent_registered1",
            "name": "Registered sample",
            "schema_id": "ts_sample",
        }
    ]
    assert found["unregistered_ids"] == ["REG-9999"]
    assert [item["container_id"] for item in found["found"]] == ["con_batch1", "con_well1"]
    assert found["found"][1]["barcode"] == "BAR-WELL"
    assert found["found"][1]["parent_storage_id"] == "plt_plate1:A1"
    assert found["truncated"] is False
    bulk = [call for call in fake.calls if call.path.endswith("registered-entities:bulk-get")]
    # Benchling rejects the whole request and names REG-9999, so the rest is read again.
    assert [call.params for call in bulk] == [
        {"entityRegistryIds": "REG-0001,REG-9999"},
        {"entityRegistryIds": "REG-0001"},
    ]
    assert {call.path for call in bulk} == {
        "/api/v2/registries/src_registry1/registered-entities:bulk-get"
    }
    assert [call.params["storageContentsIds"] for call in _container_lists(fake)] == [
        "ent_registered1"
    ]
    assert not any(call.path == "/api/v2/registries" for call in fake.calls)
    assert fake.writes() == []


async def test_find_containers_registry_defaults_to_the_only_registry(runtime, fake) -> None:
    found = await runtime.service.find_containers(registry_ids="REG-0001")
    assert found["registry"] == "src_registry1"
    assert [item["container_id"] for item in found["found"]] == ["con_batch1"]
    assert any(call.path == "/api/v2/registries" for call in fake.calls)


async def test_find_containers_several_registries_lists_them(runtime, fake) -> None:
    fake.registries.append({"id": "src_registry2", "name": "Second registry"})
    with pytest.raises(ValidationError, match="--registry") as raised:
        await runtime.service.find_containers(registry_ids=["REG-0001"])
    assert raised.value.details["registries"] == [
        {"id": "src_registry1", "name": "Synthetic registry"},
        {"id": "src_registry2", "name": "Second registry"},
    ]
    fake.registries.clear()
    with pytest.raises(ValidationError, match="0 registries") as none:
        await runtime.service.find_containers(registry_ids=["REG-0001"])
    assert none.value.details["registries"] == []
    assert _container_lists(fake) == []


async def test_find_containers_unregistered_ids_skip_the_container_list(runtime, fake) -> None:
    found = await runtime.service.find_containers(
        registry_ids=["REG-9999"], registry="src_registry1"
    )
    assert found["entities"] == found["found"] == []
    assert found["unregistered_ids"] == ["REG-9999"]
    assert _container_lists(fake) == []


async def test_find_containers_other_registry_rejections_are_raised(runtime, fake) -> None:
    fake.fail("GET", "registered-entities:bulk-get", 400)
    with pytest.raises(ValidationError):
        await runtime.service.find_containers(registry_ids=["REG-0001"], registry="src_registry1")


async def test_find_containers_malformed_registry_replies_are_upstream_errors(
    runtime, fake
) -> None:
    fake.payload_overrides.append(("/registries", 200, {"registries": "none"}))
    with pytest.raises(UpstreamError, match="registry list"):
        await runtime.service.find_containers(registry_ids=["REG-0001"])
    fake.payload_overrides.append(("registered-entities:bulk-get", 200, {"entities": [1]}))
    with pytest.raises(UpstreamError, match="registered entity list"):
        await runtime.service.find_containers(registry_ids=["REG-0001"], registry="src_registry1")


async def test_find_containers_by_contents_chunks_at_100(runtime, fake) -> None:
    entities = ["ent_sample1", *(f"ent_{index:03d}" for index in range(149))]
    found = await runtime.service.find_containers(contents=entities)
    requests = _container_lists(fake)
    assert [len(call.params["storageContentsIds"].split(",")) for call in requests] == [100, 50]
    assert all("ancestorStorageId" not in call.params for call in requests)
    assert [item["container_id"] for item in found["found"]] == ["con_source1", "con_batch1"]
    assert found["truncated"] is False


async def test_find_containers_in_storage_and_contents_send_both(runtime, fake) -> None:
    fake.add_stored_containers()
    in_box = await runtime.service.find_containers(in_storage="box_rack1")
    assert [item["container_id"] for item in in_box["found"]] == ["con_boxed1"]
    assert _container_lists(fake)[-1].params["ancestorStorageId"] == "box_rack1"
    assert "storageContentsIds" not in _container_lists(fake)[-1].params

    in_freezer = await runtime.service.find_containers(
        contents=["ent_sample1", "ent_registered1"], in_storage="loc_freezer1"
    )
    assert [item["container_id"] for item in in_freezer["found"]] == ["con_well1", "con_boxed1"]
    params = _container_lists(fake)[-1].params
    assert params["ancestorStorageId"] == "loc_freezer1"
    assert params["storageContentsIds"] == "ent_sample1,ent_registered1"

    named = await runtime.service.find_containers(name="Boxed tube", in_storage="loc_room1")
    assert [item["container_id"] for item in named["found"]] == ["con_boxed1"]
    assert _container_lists(fake)[-1].params["name"] == "Boxed tube"


async def test_find_containers_filters_follow_pages_up_to_limit(runtime, fake) -> None:
    fake.add_stored_containers()
    fake.container_page_size = 1
    found = await runtime.service.find_containers(contents=["ent_sample1"], limit=2)
    assert [item["container_id"] for item in found["found"]] == ["con_source1", "con_batch1"]
    assert found["truncated"] is True
    assert [call.params.get("nextToken") for call in _container_lists(fake)] == [None, "1", "2"]

    fake.calls.clear()
    everything = await runtime.service.find_containers(contents=["ent_sample1"], limit=3)
    assert len(everything["found"]) == 3
    assert everything["truncated"] is False


async def test_find_containers_lists_a_container_once_across_chunks(runtime, fake) -> None:
    entities = [*(f"ent_{index:03d}" for index in range(99)), "ent_sample1", "ent_registered1"]
    found = await runtime.service.find_containers(contents=entities)
    assert len(_container_lists(fake)) == 2
    assert [item["container_id"] for item in found["found"]] == ["con_source1", "con_batch1"]


@pytest.mark.parametrize(
    ("arguments", "message"),
    [
        ({"barcode": "BAR-SOURCE", "contents": ["ent_sample1"]}, "exactly one"),
        ({"name": "Source vial", "registry_ids": ["REG-0001"]}, "exactly one"),
        ({"contents": ["ent_sample1"], "registry_ids": ["REG-0001"]}, "exactly one"),
        ({"barcode": "BAR-SOURCE", "in_storage": "box_rack1"}, "--in combines"),
        ({"contents": ["ent_sample1"], "registry": "src_registry1"}, "--registry applies"),
        ({"in_storage": "box_rack1", "cursor": "50"}, "--cursor pages"),
        ({"in_storage": "con_source1"}, "--in must be"),
        ({"in_storage": " "}, "non-empty"),
        ({"contents": ["ent_a,ent_b"]}, "repeat --contents"),
        ({"registry_ids": ["REG-1,REG-2"]}, "repeat --registry-id"),
        ({"contents": []}, "non-empty"),
        ({"contents": ["ent_sample1"], "limit": 1001}, "1 and 1000"),
        ({"contents": ["ent_sample1"], "limit": True}, "1 and 1000"),
        ({"name": "Source vial", "limit": 101}, "1 and 100"),
    ],
)
async def test_find_containers_filter_input_is_checked(runtime, fake, arguments, message) -> None:
    with pytest.raises(ValidationError, match=message):
        await runtime.service.find_containers(**arguments)
    assert _container_lists(fake) == []


async def test_get_container_location_follows_well_plate_and_locations(runtime, fake) -> None:
    fake.add_stored_containers()
    plain = await runtime.service.get_container("con_well1")
    assert "location_path" not in plain
    container = await runtime.service.get_container("con_well1", location=True)
    assert container["id"] == "con_well1"
    assert container["location_path"] == [
        {"id": "plt_plate1:A1", "kind": "well", "name": "A1", "barcode": None, "schema_id": None},
        {
            "id": "plt_plate1",
            "kind": "plate",
            "name": "Synthetic plate",
            "barcode": "PLT-1",
            "schema_id": "pltsch_abc123",
        },
        {
            "id": "loc_freezer1",
            "kind": "location",
            "name": "Synthetic freezer",
            "barcode": "LOC-FREEZER",
            "schema_id": "locsch_abc123",
        },
        {
            "id": "loc_room1",
            "kind": "location",
            "name": "Synthetic room",
            "barcode": "LOC-ROOM",
            "schema_id": "locsch_abc123",
        },
    ]
    assert container["location_truncated"] is False
    reads = [call.path for call in fake.calls if not call.path.startswith("/api/v2/containers")]
    assert [path for path in reads if path != "/oauth/token"] == [
        "/api/v2/plates/plt_plate1",
        "/api/v2/locations/loc_freezer1",
        "/api/v2/locations/loc_room1",
    ]

    boxed = await runtime.service.get_container("con_boxed1", location=True)
    assert [(item["id"], item["kind"]) for item in boxed["location_path"]] == [
        ("box_rack1", "box"),
        ("loc_freezer1", "location"),
        ("loc_room1", "location"),
    ]
    top = await runtime.service.get_container("con_dest1", location=True)
    assert top["location_path"] == []
    assert top["location_truncated"] is False
    assert fake.writes() == []


async def test_get_container_location_marks_box_positions_and_unknown_parents(
    runtime, fake
) -> None:
    fake.add_stored_containers()
    fake.containers[-1]["parentStorageId"] = "box_rack1:B2"
    boxed = await runtime.service.get_container("con_boxed1", location=True)
    assert boxed["location_path"][0] == {
        "id": "box_rack1:B2",
        "kind": "position",
        "name": "B2",
        "barcode": None,
        "schema_id": None,
    }
    assert boxed["location_path"][1]["id"] == "box_rack1"

    fake.storage["loc_room1"]["parentStorageId"] = "zzz_unknown"
    unknown = await runtime.service.get_container("con_boxed1", location=True)
    assert unknown["location_path"][-1] == {
        "id": "zzz_unknown",
        "kind": None,
        "name": None,
        "barcode": None,
        "schema_id": None,
    }
    assert unknown["location_truncated"] is False


async def test_get_container_location_stops_after_ten_levels_on_a_cycle(runtime, fake) -> None:
    fake.add_stored_containers()
    fake.storage["loc_room1"]["parentStorageId"] = "box_rack1"
    container = await runtime.service.get_container("con_boxed1", location=True)
    assert len(container["location_path"]) == 10
    assert container["location_truncated"] is True
    storage_reads = [
        call
        for call in fake.calls
        if call.path.startswith(("/api/v2/boxes", "/api/v2/locations", "/api/v2/plates"))
    ]
    assert len(storage_reads) == 10


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
