"""Notebook text boxes and lookup tables read as first-class document parts."""

from __future__ import annotations

import pytest
from fake_benchling import ENTRY_ID, TABLE_PART, TEXT_PART
from test_mcp_server import Session, call

from benchling_tools.errors import ConflictError, NotFoundError, UpstreamError
from benchling_tools.mcp_server import build_mcp_server
from benchling_tools.services import is_lookup_table, is_text_box

LOOKUP = "docpart_lookup1"
BOX = "docpart_box1"
OPEN_BOX = "docpart_box2"


@pytest.fixture
def client(pool):
    return Session(build_mcp_server(pool))


def _entry_reads(fake) -> list:
    return [c for c in fake.calls if c.path.endswith(f"/entry/{ENTRY_ID}")]


def _mixed_entry(fake) -> None:
    """Results tables, lookup tables, and a submitted and an unsubmitted text box."""
    fake.parts = [part for part in fake.parts if part["id"] != "docpart_struct1"]
    fake.add_example_results_part()
    fake.add_text_box(BOX)
    fake.add_text_box(
        OPEN_BOX,
        name="Reviewer notes",
        text="",
        submission_status="UNSUBMITTED",
        is_submittable=True,
        rich=False,
    )
    fake.add_lookup_table(LOOKUP, rows=3)
    fake.lookup_row_page_size = 2


def _lookup_row_calls(fake) -> list:
    return [
        c for c in fake.calls if c.path.endswith(f"/document-part-lookup-table/{LOOKUP}/rows/items")
    ]


def test_part_type_predicates() -> None:
    assert is_text_box({"__typename": "DocumentPartTextBox"})
    assert is_text_box({"content": {"__typename": "TextBoxDocumentContent"}})
    assert not is_text_box({"__typename": "DocumentPartText"})
    assert is_lookup_table({"__typename": "benchling.DocumentPartLookupTable"})
    assert not is_lookup_table({"__typename": "DocumentPartResultsTable"})


async def test_entry_with_text_boxes_and_lookup_tables_is_complete(runtime, fake) -> None:
    _mixed_entry(fake)
    inspected = await runtime.service.inspect_entry(ENTRY_ID)
    assert inspected["completeness"] == "complete", inspected["issues"]
    assert inspected["issues"] == []
    assert inspected["text_boxes"] == [
        {
            "part_id": BOX,
            "name": "Comments",
            "text": "Plate 2 looked cloudy; see ",
            "submission_status": "SUBMITTED",
            "is_submittable": False,
            "section": {"part_id": "docpart_sec2", "title": "Results"},
        },
        {
            "part_id": OPEN_BOX,
            "name": "Reviewer notes",
            "text": "",
            "submission_status": "UNSUBMITTED",
            "is_submittable": True,
            "section": {"part_id": "docpart_sec2", "title": "Results"},
        },
    ]
    assert inspected["lookup_tables"] == [
        {"part_id": LOOKUP, "name": "Reagent lookup", "row_count": 3}
    ]
    assert inspected["tables"][LOOKUP]["type"] == "DocumentPartLookupTable"
    assert {"docpart_example", TABLE_PART} <= set(inspected["tables"])
    # Three rows at two per page is two row pages, read without a stale check.
    assert len(_lookup_row_calls(fake)) == 2
    assert all(c.params.get("pageSize") == "100" for c in _lookup_row_calls(fake))
    assert not [c for c in fake.calls if "/result/" in c.path and "lookup" in c.path]
    assert fake.writes() == []


async def test_text_box_before_any_section_has_no_section(runtime, fake) -> None:
    fake.add_text_box(BOX)
    fake.parts.insert(0, fake.parts.pop())
    inspected = await runtime.service.inspect_entry(ENTRY_ID)
    assert inspected["text_boxes"][0]["section"] is None


async def test_snapshot_lookup_rows_keep_typed_cells(runtime, fake) -> None:
    _mixed_entry(fake)
    snapshot = await runtime.service.get_entry(ENTRY_ID, full=True)
    table = snapshot["tables"][LOOKUP]
    assert table["rowCount"] == 3
    assert [col["name"] for col in table["content"]["columnDefinitions"]] == ["Lot", "Uses"]
    assert table["content"]["rows"][2]["cells"] == [
        {"inputValue": {"type": "text_value", "value": "LOT-2"}},
        {"inputValue": {"type": "integer_value", "value": 2}},
    ]
    box = next(part for part in snapshot["parts"] if part["id"] == BOX)
    assert box["text"] == "Plate 2 looked cloudy; see "


async def test_lookup_table_with_bad_columns_fails_the_read(runtime, fake) -> None:
    fake.add_lookup_table(LOOKUP)
    fake.lookup_parts[LOOKUP]["columnDefinitions"] = ["not a column"]
    with pytest.raises(UpstreamError, match="invalid columns") as raised:
        await runtime.service.get_lookup_table_part(LOOKUP)
    assert raised.value.details == {"part_id": LOOKUP, "type": "DocumentPartLookupTable"}


def _snapshot_calls(fake) -> list:
    """Calls only a full snapshot makes: an unfiltered parts list, results, and V2 fields."""
    return [
        c
        for c in fake.calls
        if (c.path.endswith("/parts/items") and "id.anyOf" not in c.params)
        or c.path.endswith("/result/items")
        or "/api/v2/entries/" in c.path
    ]


async def test_part_get_reads_one_text_box_without_a_snapshot(runtime, fake) -> None:
    _mixed_entry(fake)
    data = await runtime.service.get_part(ENTRY_ID, BOX)
    assert data["entry_id"] == ENTRY_ID
    assert data["version_id"] == "ver_1"
    assert data["detail"] is None
    box = data["text_box"]
    assert box["part_id"] == BOX
    assert box["name"] == "Comments"
    assert box["text"] == "Plate 2 looked cloudy; see "
    assert box["formats"] == [
        {"__typename": "TextFormat", "start": 0, "end": 7, "properties": ["BOLD"]}
    ]
    assert box["links"][0]["url"] == "https://example.test/notes"
    assert box["mentions"] == [{"__typename": "Mention", "itemId": "ent_target", "position": 27}]
    assert box["submission_status"] == "SUBMITTED"
    assert box["is_submittable"] is False
    parts_calls = [c for c in fake.calls if c.path.endswith("/parts/items")]
    assert [c.params["id.anyOf"] for c in parts_calls] == [BOX]
    assert _snapshot_calls(fake) == []
    # The part carries the entry version, so only resolving the entry reads it.
    assert len(_entry_reads(fake)) == 1


async def test_part_get_reads_one_lookup_table_and_every_row_page(runtime, fake) -> None:
    _mixed_entry(fake)
    data = await runtime.service.get_part(ENTRY_ID, LOOKUP)
    assert data["version_id"] == "ver_1"
    assert "text_box" not in data
    assert data["detail"]["rowCount"] == 3
    assert len(data["detail"]["content"]["rows"]) == 3
    assert len(_lookup_row_calls(fake)) == 2
    assert _snapshot_calls(fake) == []
    assert not [c for c in fake.calls if "/document-part-results-table/" in c.path]


async def test_part_get_without_part_version_reads_the_entry_version(runtime, fake) -> None:
    data = await runtime.service.get_part(ENTRY_ID, TEXT_PART)
    assert data["version_id"] == "ver_1"
    assert len(_entry_reads(fake)) == 2
    assert _snapshot_calls(fake) == []


async def test_part_get_falls_back_to_the_snapshot_when_the_filter_is_rejected(
    runtime, fake
) -> None:
    _mixed_entry(fake)
    fake.reject_part_id_filter = True
    data = await runtime.service.get_part(ENTRY_ID, BOX)
    assert data["text_box"]["name"] == "Comments"
    assert data["version_id"] == "ver_1"
    lookup = await runtime.service.get_part(ENTRY_ID, LOOKUP)
    assert lookup["detail"]["rowCount"] == 3
    assert any(c.path.endswith("/parts/items") and "id.anyOf" not in c.params for c in fake.calls)
    with pytest.raises(NotFoundError):
        await runtime.service.get_part(ENTRY_ID, "docpart_missing")


async def test_part_get_rejects_a_duplicate_part(runtime, fake) -> None:
    fake.add_text_box(BOX)
    fake.parts.append(dict(fake.parts[-1]))
    with pytest.raises(UpstreamError, match="duplicate"):
        await runtime.service.get_part(ENTRY_ID, BOX)


async def test_part_get_retries_once_then_reports_a_version_change(runtime, fake) -> None:
    fake.parts[3]["versionMetadata"] = {"versionId": "ver_0"}
    with pytest.raises(ConflictError) as raised:
        await runtime.service.get_part(ENTRY_ID, TABLE_PART)
    assert raised.value.details["part_version"] == "ver_0"
    assert raised.value.details["table_version"] == "ver_1"
    assert len([c for c in fake.calls if c.path.endswith("/parts/items")]) == 2


async def test_section_get_includes_text_boxes_and_lookup_tables(runtime, fake) -> None:
    _mixed_entry(fake)
    section = await runtime.service.get_section(ENTRY_ID, "Results")
    assert [item["part_id"] for item in section["text_boxes"]] == [BOX, OPEN_BOX]
    assert section["text_boxes"][0]["section"] == {"part_id": "docpart_sec2", "title": "Results"}
    assert section["lookup_tables"] == [
        {"part_id": LOOKUP, "name": "Reagent lookup", "row_count": 3}
    ]
    methods = await runtime.service.get_section(ENTRY_ID, "Methods")
    assert methods["text_boxes"] == [] and methods["lookup_tables"] == []


async def test_mcp_tools_return_text_boxes_and_lookup_tables(client, fake) -> None:
    _mixed_entry(fake)
    inspected = (await call(client, "entry_inspect", env="sandbox", reference=ENTRY_ID))["data"]
    assert inspected["completeness"] == "complete"
    assert [item["part_id"] for item in inspected["text_boxes"]] == [BOX, OPEN_BOX]
    assert inspected["lookup_tables"][0]["row_count"] == 3
    paged = (
        await call(
            client,
            "section_get",
            env="sandbox",
            reference=ENTRY_ID,
            section="Results",
            part_offset=5,
            table_row_limit=1,
        )
    )["data"]
    assert [part["id"] for part in paged["parts"]] == [OPEN_BOX, LOOKUP]
    assert [item["part_id"] for item in paged["text_boxes"]] == [OPEN_BOX]
    assert paged["lookup_tables"][0]["row_count"] == 3
    assert paged["tables"][LOOKUP]["pagination"] == {
        "offset": 0,
        "returned": 1,
        "total": 3,
        "next_offset": 1,
    }
    box = (await call(client, "part_get", env="sandbox", reference=ENTRY_ID, part_id=BOX))["data"]
    assert box["text_box"]["submission_status"] == "SUBMITTED"
    assert box["detail"] is None
    lookup = (
        await call(
            client,
            "part_get",
            env="sandbox",
            reference=ENTRY_ID,
            part_id=LOOKUP,
            row_offset=2,
            row_limit=5,
        )
    )["data"]
    assert lookup["detail"]["pagination"] == {
        "offset": 2,
        "returned": 1,
        "total": 3,
        "next_offset": None,
    }
    assert lookup["detail"]["content"]["rows"][0]["cells"][0]["inputValue"]["value"] == "LOT-2"
    assert fake.writes() == []
