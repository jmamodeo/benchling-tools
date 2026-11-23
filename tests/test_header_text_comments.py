"""A labeled area built from a header and text paragraphs is not a text box.

Some templates label a free-text area (for example "Comments") with a
DocumentPartHeader and leave DocumentPartText paragraphs under it, instead of a
DocumentPartTextBox. Both are documented part types in List entry parts. The
area must read as complete, list no text box, and keep the header and every
paragraph, empty ones included, in order.
"""

from __future__ import annotations

from fake_benchling import ENTRY_ID

SECTION = "docpart_secC"
HEADER = "docpart_hdrC"
PARAS = ["docpart_paraC1", "docpart_paraC2", "docpart_paraC3"]


def _comments_area(fake, texts: list[str]) -> None:
    version = {"versionId": fake.version}
    fake.parts = [part for part in fake.parts if part["id"] != "docpart_struct1"]
    fake.parts.append({"id": SECTION, "__typename": "DocumentPartSection", "title": "Conclusion"})
    fake.parts.append(
        {
            "id": HEADER,
            "__typename": "DocumentPartHeader",
            "text": "Comments",
            "headerType": "SUBHEADER_1",
            "formats": [],
            "links": [],
            "mentions": [],
            "indentation": 0,
            "versionMetadata": version,
        }
    )
    for part_id, text in zip(PARAS, texts, strict=True):
        fake.parts.append(
            {
                "id": part_id,
                "__typename": "DocumentPartText",
                "text": text,
                "formats": [],
                "links": [],
                "mentions": [],
                "indentation": 0,
                "versionMetadata": version,
            }
        )


async def test_header_and_paragraphs_are_complete_with_no_text_box(runtime, fake) -> None:
    _comments_area(fake, ["", "", ""])
    inspected = await runtime.service.inspect_entry(ENTRY_ID)
    assert inspected["completeness"] == "complete", inspected["issues"]
    assert inspected["text_boxes"] == []
    conclusion = next(s for s in inspected["sections"] if s["part_id"] == SECTION)
    assert conclusion["title"] == "Conclusion"
    assert conclusion["child_part_ids"] == [HEADER, *PARAS]


async def test_section_get_returns_the_header_and_every_paragraph(runtime, fake) -> None:
    _comments_area(fake, ["Plate 3 re-read.", "", "Lot changed."])
    data = await runtime.service.get_section(ENTRY_ID, "Conclusion")
    assert data["text_boxes"] == []
    body = [p for p in data["parts"] if p.get("id") != SECTION]
    assert [p["id"] for p in body] == [HEADER, *PARAS]
    assert body[0]["__typename"] == "DocumentPartHeader"
    assert body[0]["headerType"] == "SUBHEADER_1"
    assert body[0]["text"] == "Comments"
    assert [p["text"] for p in body[1:]] == ["Plate 3 re-read.", "", "Lot changed."]


async def test_part_get_reads_a_header_and_an_empty_paragraph(runtime, fake) -> None:
    _comments_area(fake, ["", "", ""])
    header = await runtime.service.get_part(ENTRY_ID, HEADER)
    assert header["part"]["text"] == "Comments"
    assert "text_box" not in header
    para = await runtime.service.get_part(ENTRY_ID, PARAS[0])
    assert para["part"]["__typename"] == "DocumentPartText"
    assert para["part"]["text"] == ""
    assert "text_box" not in para
