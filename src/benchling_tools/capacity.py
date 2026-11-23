"""Results-table row capacity: how many rows a notebook results table can still take.

Benchling limits a results table to 1,000 rows. ``changes plan --family results``
and ``results capacity`` use the same reads and arithmetic here, so the report is
the check a plan runs. Rows come from V3 ``rowCount`` on
``GET /document-part-results-table/{id}``. The V2 ``strtbl_…`` table is matched to
its V3 part with ``BenchlingService.document_part_for_listed_table``.

Benchling does not document whether pending results (created through the API but
not yet accepted with the plug icon) count toward the limit, so both numbers are
reported and neither is assumed.
"""

from __future__ import annotations

from collections.abc import Collection, Mapping
from typing import Any

from .errors import UpstreamError
from .export import _cell_value
from .services import BenchlingService, rows_items_path
from .stale import row_result_id

# Benchling Help, "Capture data with Results" (FAQ): "Result tables cannot have more
# than 1000 rows."
# https://help.benchling.com/hc/en-us/articles/39954660101773-Capture-data-with-Results
# "Standardize data capture with structured tables": "Tables are limited to 1,000 rows
# and 255 columns."
# https://help.benchling.com/hc/en-us/articles/39940813149709-Standardize-data-capture-with-structured-tables
RESULTS_TABLE_ROW_LIMIT = 1000

# This toolkit's own batch size for one table in one plan. It is not a Benchling
# limit: V2 POST /assay-results:bulk-create documents "Limit of 4000 results per
# request."
# https://docs.benchling.com/docs/api/v2/benchling-api/assay-results/bulk-create-assay-results
TOOLKIT_RESULTS_PER_TABLE = 500
BULK_CREATE_RESULTS_PER_REQUEST = 4000

TOOLKIT_BATCH_MESSAGE = (
    f"This toolkit uploads at most {TOOLKIT_RESULTS_PER_TABLE} results per table in one "
    "plan; split the rows into several plans. Benchling documents "
    f"{BULK_CREATE_RESULTS_PER_REQUEST:,} results per bulk-create request."
)

UNKNOWN_PENDING_WARNING = (
    "Benchling does not document whether pending results count toward the "
    f"{RESULTS_TABLE_ROW_LIMIT:,}-row table limit. These rows fit only if the pending "
    "results this home knows about do not count"
)

_EMPTY: tuple[Any, ...] = (None, "", [])


def is_placeholder_row(row: Any) -> bool:
    """True for an empty, unsubmitted row with no result: a new table's blank row."""
    if not isinstance(row, dict) or row.get("isArchived") or row_result_id(row):
        return False
    if row.get("submissionStatus") != "UNSUBMITTED":
        return False
    cells = row.get("cells")
    if not isinstance(cells, list):
        return False
    return all(isinstance(cell, dict) and _cell_value(cell) in _EMPTY for cell in cells)


async def read_table_usage(
    service: BenchlingService,
    entry_id: str,
    table_ids: Collection[str] | None = None,
    *,
    tables: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Rows used by each V2 results table on the entry, or by those in ``table_ids``.

    The entry's V2 tables and V3 parts are each read once; ``tables`` skips the V2
    read when the caller already listed them. ``rowCount`` and ``submissionStatus``
    come from the parts listing (``DocumentPartResultsTable`` carries both); a part
    listed without a usable ``rowCount`` is read with
    ``GET /document-part-results-table/{id}``. Only when ``rowCount`` is 1 and the table is not ``SUBMITTED`` is the
    first row read, to tell a blank placeholder row from a real one. A table with
    no single matching part has ``row_count`` null.
    """
    if tables is None:
        entry = service._v2_object(
            await service.api.request("GET", f"/entries/{entry_id}", version=2), "entry"
        )
        tables = service._result_tables_from_entry(entry)
    if table_ids is not None:
        wanted = set(table_ids)
        tables = [table for table in tables if table["table_id"] in wanted]
    if not tables:
        return []
    parts = [
        item
        async for item in service.api.paginate(
            f"/entry/{entry_id}/parts/items", params={"pageSize": 100}
        )
    ]
    by_id = {str(part["id"]): part for part in parts if isinstance(part.get("id"), str)}
    usage: list[dict[str, Any]] = []
    for table in tables:
        part_id = await service.document_part_for_listed_table(
            entry_id, table, unstructured=False, parts=parts
        )
        item: dict[str, Any] = {
            "table_id": table["table_id"],
            "part_id": part_id,
            "name": table.get("name"),
            "schema_id": table.get("schema_id"),
            "row_count": None,
            "submission_status": None,
            "placeholder_rows": 0,
            "used_rows": None,
        }
        if part_id is not None:
            detail = by_id.get(part_id, {})
            if not _usable_row_count(detail.get("rowCount")) or not detail.get("rows"):
                detail = await service.api.request("GET", f"/document-part-results-table/{part_id}")
            row_count = detail.get("rowCount")
            if isinstance(row_count, bool) or not isinstance(row_count, int) or row_count < 0:
                raise UpstreamError(
                    "Table response contained an invalid row count",
                    details={"part_id": part_id},
                )
            item["row_count"] = row_count
            item["submission_status"] = detail.get("submissionStatus")
            # A placeholder row is UNSUBMITTED, so a SUBMITTED table cannot hold one.
            if row_count == 1 and item["submission_status"] != "SUBMITTED":
                path = rows_items_path(
                    service.api.environment.base_url,
                    part_id,
                    detail.get("rows"),
                    collection="document-part-results-table",
                )
                page = await service.api.request("GET", path, params={"pageSize": 1})
                rows = page.get("items")
                if isinstance(rows, list) and len(rows) == 1 and is_placeholder_row(rows[0]):
                    item["placeholder_rows"] = 1
            item["used_rows"] = row_count - item["placeholder_rows"]
        usage.append(item)
    return usage


def _usable_row_count(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


async def add_table_counts(
    service: BenchlingService, entry_id: str, tables: list[dict[str, Any]]
) -> None:
    """Add ``part_id``, ``row_count``, ``used_rows``, ``submission_status`` and
    ``placeholder_rows`` to each listed V2 results table, in place.

    This is ``read_table_usage`` over tables the caller already listed, so
    ``results tables`` reports the same numbers as ``results capacity``. A table
    no single part matches gets null counts and ``count_note``.
    """
    if not tables:
        return
    usage = {
        item["table_id"]: item for item in await read_table_usage(service, entry_id, tables=tables)
    }
    for table in tables:
        item = usage.get(table["table_id"], {})
        if item.get("part_id") is None:
            table.update(
                {
                    "part_id": None,
                    "row_count": None,
                    "used_rows": None,
                    "submission_status": None,
                    "placeholder_rows": None,
                    "count_note": (
                        "No single v3_document_part matched this table, so it was not counted."
                    ),
                }
            )
            continue
        status = item.get("submission_status")
        table.update(
            {
                "part_id": item["part_id"],
                "row_count": item["row_count"],
                "used_rows": item["used_rows"],
                "submission_status": status if isinstance(status, str) else None,
                "placeholder_rows": item["placeholder_rows"],
            }
        )


def table_capacity(usage: Mapping[str, Any], pending_known: int, planned: int) -> dict[str, Any]:
    """One table's capacity numbers and whether ``planned`` rows fit.

    ``fits`` is true when the rows fit even if pending results count, false when
    they do not fit even if pending results are ignored, ``"unknown_pending"``
    between the two, and ``"unknown"`` when the row count could not be read.
    """
    used = usage.get("used_rows")
    report = {
        **usage,
        "pending_known": pending_known,
        "planned": planned,
        "limit": RESULTS_TABLE_ROW_LIMIT,
        "remaining_without_pending": None,
        "remaining_with_pending": None,
    }
    if not isinstance(used, int):
        report["fits"] = "unknown"
        return report
    without_pending = RESULTS_TABLE_ROW_LIMIT - used - planned
    with_pending = without_pending - pending_known
    report["remaining_without_pending"] = without_pending
    report["remaining_with_pending"] = with_pending
    if without_pending < 0:
        report["fits"] = False
    elif with_pending >= 0:
        report["fits"] = True
    else:
        report["fits"] = "unknown_pending"
    return report


def combine(
    usage: list[dict[str, Any]],
    pending_known: Mapping[str, int],
    planned: Mapping[str, int],
) -> dict[str, Any]:
    """Per-table capacity, the overall ``fits``, and warnings for unknowns."""
    tables = [
        table_capacity(
            item, pending_known.get(item["table_id"], 0), planned.get(item["table_id"], 0)
        )
        for item in usage
    ]
    states = [table["fits"] for table in tables]
    fits: bool | str
    if False in states:
        fits = False
    elif "unknown_pending" in states:
        fits = "unknown_pending"
    elif "unknown" in states:
        fits = "unknown"
    else:
        fits = True
    warnings: list[str] = []
    uncertain = [label(table) for table in tables if table["fits"] == "unknown_pending"]
    if uncertain:
        warnings.append(f"{UNKNOWN_PENDING_WARNING}: {', '.join(uncertain)}")
    unread = [label(table) for table in tables if table["fits"] == "unknown"]
    if unread:
        warnings.append(
            "The row count could not be read because no single V3 results-table part "
            f"matches: {', '.join(unread)}. Its capacity was not checked"
        )
    return {
        "limit": RESULTS_TABLE_ROW_LIMIT,
        "tables": tables,
        "fits": fits,
        "warnings": warnings,
    }


def over_limit(report: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Tables whose rows do not fit even when pending results are ignored."""
    return [table for table in report["tables"] if table["fits"] is False]


def next_step(report: Mapping[str, Any]) -> str | None:
    """What to do when a table does not fit, or may not fit because of pending rows."""
    full = over_limit(report)
    if full:
        names = ", ".join(label(table) for table in full)
        return (
            f"{names} cannot take the planned rows within Benchling's "
            f"{RESULTS_TABLE_ROW_LIMIT:,}-row table limit. Put the rows in a new entry or "
            "another results table, or plan fewer rows."
        )
    uncertain = [table for table in report["tables"] if table["fits"] == "unknown_pending"]
    if uncertain:
        names = ", ".join(label(table) for table in uncertain)
        return (
            f"{names} has room only if pending results do not count. Accept or archive "
            "the pending results first, or use a new entry or another results table."
        )
    return None


def label(table: Mapping[str, Any]) -> str:
    name = table.get("name")
    return f'"{name}" ({table["table_id"]})' if name else str(table["table_id"])
