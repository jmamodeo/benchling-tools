"""Read back submitted results, mark pending ones, and diff them against a result plan.

Benchling V2 assay results have ``entryId`` set to null until a person accepts them
into the entry's results table (the plug icon in the table toolbar). These helpers
only read; they never write to Benchling or to the local journal.
"""

from __future__ import annotations

from typing import Any

from ..errors import ValidationError
from ..models import ChangeJournal, ChangePlan
from .blobs import local_file_descriptor

PENDING = "pending"
ACCEPTED = "accepted"
ARCHIVED = "archived"
# "Limit of 4000 results per request" (V2 Bulk create assay results).
BULK_CREATE_LIMIT = 4000
_STATE_ORDER = {PENDING: 0, ACCEPTED: 1, ARCHIVED: 2}

NO_ENTRY_PLANS_NEXT_STEP = (
    "No applied result upload plan for this entry was found in this BENCHLING_TOOLS_HOME; "
    "only plans applied from this home are known. For a plan applied from another home, run "
    "`results pending --plan BUNDLE.json` with a bundle made there by `changes export PLAN_ID "
    "--output BUNDLE.json`, or run `results pending --schema SCHEMA_ID --created-after TIME`."
)


def result_state(record: dict[str, Any]) -> str:
    """``archived`` when archived, ``pending`` with no entry, otherwise ``accepted``."""
    if record.get("archiveRecord"):
        return ARCHIVED
    return ACCEPTED if record.get("entryId") else PENDING


def field_values(record: dict[str, Any]) -> dict[str, Any]:
    """Map each V2 field system name to its ``value``."""
    fields = record.get("fields")
    if not isinstance(fields, dict):
        return {}
    return {
        str(name): value.get("value") if isinstance(value, dict) else value
        for name, value in fields.items()
    }


def record_schema_id(record: dict[str, Any]) -> str | None:
    schema = record.get("schema")
    if isinstance(schema, dict) and isinstance(schema.get("id"), str):
        return str(schema["id"])
    schema_id = record.get("schemaId")
    return schema_id if isinstance(schema_id, str) else None


def summarize_record(record: dict[str, Any], **extra: Any) -> dict[str, Any]:
    """Compact view of one fetched V2 result with its pending or accepted state.

    ``id`` and ``v2_id`` are the V2 assay result UUID; ``v3_id`` is the V3 ``result_…`` id.
    """
    state = result_state(record)
    return {
        "id": record.get("id"),
        "v2_id": record.get("id"),
        "v3_id": record.get("v3Id"),
        "schema_id": record_schema_id(record),
        "entry_id": record.get("entryId"),
        "state": state,
        "needs_accept": state == PENDING,
        "is_reviewed": record.get("isReviewed"),
        "created_at": record.get("createdAt"),
        **extra,
        "fields": field_values(record),
    }


def sort_by_state(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Pending first, so a bounded sample shows the rows that need action."""
    return sorted(items, key=lambda item: _STATE_ORDER.get(str(item.get("state")), 3))


def state_counts(items: list[dict[str, Any]]) -> dict[str, int]:
    counts = {PENDING: 0, ACCEPTED: 0, ARCHIVED: 0}
    for item in items:
        state = str(item.get("state"))
        counts[state] = counts.get(state, 0) + 1
    return counts


def pending_next_step(
    pending: int,
    *,
    entry_id: str | None = None,
    table_names: list[str] | None = None,
    web_url: str | None = None,
) -> str | None:
    """Tell the user how to accept pending results, or None when nothing is pending."""
    if pending <= 0:
        return None
    noun = "result is" if pending == 1 else "results are"
    entry = f"entry {entry_id} at {web_url}" if entry_id and web_url else f"entry {entry_id}"
    if entry_id and table_names:
        tables = "tables" if len(table_names) > 1 else "table"
        names = ", ".join(f'"{name}"' for name in table_names)
        where = f"{entry}, go to the result {tables} {names},"
    elif entry_id:
        where = f"{entry}, go to its results table,"
    else:
        where = "the entry the results were uploaded to, go to its results table,"
    return (
        f"{pending} {noun} pending and still need to be accepted. Open {where} and click "
        "the plug (integrate) icon in the table toolbar. Pending results have no entry yet, "
        "so `results list --entry` and `results rows` do not show them until they are accepted."
    )


def values_equal(planned: Any, actual: Any) -> bool:
    """Compare a planned field value with a fetched one.

    Numbers compare by value (12 equals 12.0). Lists of strings compare without order.
    """
    if isinstance(planned, bool) or isinstance(actual, bool):
        return type(planned) is type(actual) and planned == actual
    numeric = (int, float)
    if isinstance(planned, numeric) and isinstance(actual, numeric):
        return float(planned) == float(actual)
    if isinstance(planned, list) and isinstance(actual, list):
        if len(planned) != len(actual):
            return False
        if all(values_equal(a, b) for a, b in zip(planned, actual)):
            return True
        if all(isinstance(item, str) for item in [*planned, *actual]):
            return sorted(planned) == sorted(actual)
        return False
    return bool(planned == actual)


def field_mismatches(
    planned_fields: dict[str, Any], record: dict[str, Any], schema_id: str | None
) -> list[dict[str, Any]]:
    """List planned fields, by system name, whose fetched value differs."""
    problems: list[dict[str, Any]] = []
    actual_schema = record_schema_id(record)
    if schema_id and actual_schema and actual_schema != schema_id:
        problems.append({"field": "schemaId", "planned": schema_id, "actual": actual_schema})
    actual = field_values(record)
    for name, spec in planned_fields.items():
        planned_value = spec.get("value") if isinstance(spec, dict) else spec
        if name not in actual:
            if planned_value is not None:
                problems.append(
                    {"field": name, "planned": planned_value, "actual": None, "present": False}
                )
            continue
        if not values_equal(planned_value, actual[name]):
            problems.append({"field": name, "planned": planned_value, "actual": actual[name]})
    return problems


def uploaded_blob_ids(journal: ChangeJournal) -> dict[str, str]:
    """Blob ID per planned local-file ref, from the journal's successful uploads."""
    uploaded: dict[str, str] = {}
    for event in journal.events:
        if event.stage != "upload_blob" or event.status != "succeeded":
            continue
        ref, blob_id = event.details.get("ref"), event.details.get("blob_id")
        if isinstance(ref, str) and isinstance(blob_id, str):
            uploaded[ref] = blob_id
    return uploaded


def _with_blob_ids(value: Any, blob_ids: dict[str, str]) -> Any:
    """Replace planned local-file descriptors that were uploaded with their blob IDs."""
    if isinstance(value, list):
        return [_with_blob_ids(item, blob_ids) for item in value]
    descriptor = local_file_descriptor(value)
    if descriptor is not None and descriptor["ref"] in blob_ids:
        return blob_ids[descriptor["ref"]]
    return value


def _records_with_blob_ids(records: list[Any], blob_ids: dict[str, str]) -> list[Any]:
    out: list[Any] = []
    for record in records:
        fields = record.get("fields") if isinstance(record, dict) else None
        if not isinstance(fields, dict):
            out.append(record)
            continue
        out.append(
            {
                **record,
                "fields": {
                    name: {**spec, "value": _with_blob_ids(spec.get("value"), blob_ids)}
                    if isinstance(spec, dict) and "value" in spec
                    else _with_blob_ids(spec, blob_ids)
                    for name, spec in fields.items()
                },
            }
        )
    return out


def planned_batches(
    plan: ChangePlan, blob_ids: dict[str, str] | None = None
) -> list[dict[str, Any]]:
    """The planned result batches with table, schema, records and plan row offset.

    ``blob_ids`` maps planned local-file refs to the blob IDs apply uploaded, so an
    attachment field is compared by the blob ID Benchling holds, not by the file.
    """
    after = plan.after or {}
    batches = after.get("batches")
    if not isinstance(batches, list):
        table = (plan.before or {}).get("table") or {}
        batches = [
            {
                "table_id": after.get("table_id"),
                "schema_id": after.get("schema_id")
                or (plan.preconditions or {}).get("result_schema_id"),
                "table": table,
                "records": after.get("records") or [],
                "plan_row_offset": 0,
            }
        ]
    planned: list[dict[str, Any]] = []
    for batch in batches:
        if not isinstance(batch, dict):
            continue
        table = batch.get("table") if isinstance(batch.get("table"), dict) else {}
        raw_records = batch.get("records")
        records: list[Any] = raw_records if isinstance(raw_records, list) else []
        if blob_ids:
            records = _records_with_blob_ids(records, blob_ids)
        planned.append(
            {
                "table_id": batch.get("table_id"),
                "schema_id": batch.get("schema_id"),
                "table_name": (table or {}).get("name") or batch.get("table_id"),
                "records": records,
                "plan_row_offset": batch.get("plan_row_offset", 0),
            }
        )
    return planned


def result_ids(record: dict[str, Any]) -> dict[str, Any]:
    """``result_id`` (the V2 id, as before) with ``v2_id`` and ``v3_id`` for one record."""
    return {"result_id": record.get("id"), "v2_id": record.get("id"), "v3_id": record.get("v3Id")}


def diff_batch(
    batch: dict[str, Any], fetched: list[dict[str, Any]], *, missing_reason: str
) -> dict[str, list[dict[str, Any]]]:
    """Pair planned rows with fetched records and classify each pair.

    Exact field matches are paired first, so the result does not depend on the order
    Benchling returns records in. Remaining rows are paired in order and reported as
    mismatched; unpaired planned rows are missing and unpaired records are extra.
    A batch with ``row_indexes`` reports each row's index from that list instead of
    ``plan_row_offset`` plus its position.
    """
    table_id = batch.get("table_id")
    schema_id = batch.get("schema_id")
    offset = batch.get("plan_row_offset", 0)
    offset = offset if isinstance(offset, int) and not isinstance(offset, bool) else 0
    indexes = batch.get("row_indexes")
    remaining = list(fetched)
    matched: list[dict[str, Any]] = []
    unmatched: list[tuple[int, dict[str, Any]]] = []
    for position, planned in enumerate(batch.get("records") or []):
        row_index = (
            indexes[position]
            if isinstance(indexes, list) and position < len(indexes)
            else offset + position
        )
        fields = planned.get("fields") if isinstance(planned, dict) else None
        fields = fields if isinstance(fields, dict) else {}
        hit = next(
            (
                index
                for index, record in enumerate(remaining)
                if not field_mismatches(fields, record, schema_id)
            ),
            None,
        )
        if hit is None:
            unmatched.append((row_index, fields))
            continue
        record = remaining.pop(hit)
        matched.append(
            {
                "table_id": table_id,
                "plan_row_index": row_index,
                **result_ids(record),
                "state": result_state(record),
            }
        )
    mismatched = [
        {
            "table_id": table_id,
            "plan_row_index": row_index,
            **result_ids(record),
            "state": result_state(record),
            "fields": field_mismatches(fields, record, schema_id),
        }
        for (row_index, fields), record in zip(unmatched, remaining)
    ]
    missing = [
        {"table_id": table_id, "plan_row_index": row_index, "reason": missing_reason}
        for row_index, _fields in unmatched[len(remaining) :]
    ]
    extra = [
        {"table_id": table_id, **result_ids(record), "state": result_state(record)}
        for record in remaining[len(unmatched) :]
    ]
    return {"matched": matched, "mismatched": mismatched, "missing": missing, "extra": extra}


def _payload_bodies(payload: Any) -> list[dict[str, Any]]:
    """The bulk-create bodies in a payload: one body, a bare result list, or a list of bodies."""
    if isinstance(payload, dict):
        return [payload]
    if isinstance(payload, list):
        if payload and all(isinstance(item, dict) and "assayResults" in item for item in payload):
            return list(payload)
        if any(isinstance(item, dict) and "assayResults" in item for item in payload):
            raise ValidationError(
                "A payload list must hold only result objects or only bulk-create bodies"
            )
        return [{"assayResults": payload}]
    raise ValidationError(
        "A payload must be a bulk-create body {assayResults: [...]}, a list of result "
        "objects, or a list of bulk-create bodies"
    )


def payload_batches(payload: Any) -> list[dict[str, Any]]:
    """Batches in the shape ``planned_batches`` returns, from an SDK bulk-create payload.

    ``payload`` is a V2 bulk-create body (``assayResults`` and an optional ``tableId``),
    a bare list of result objects, or a list of bodies. Rows are grouped by ``schemaId``
    across bodies, in order; ``row_indexes`` gives each row's index in the whole payload,
    counting from 0 across bodies. ``table_id`` is the bodies' ``tableId`` when they
    agree, otherwise None. Fields are used as given, by system name. Error details name
    positions only, never values.
    """
    groups: dict[str, dict[str, Any]] = {}
    row_index = 0
    for body_index, body in enumerate(_payload_bodies(payload)):
        records = body.get("assayResults") if isinstance(body, dict) else None
        if not isinstance(records, list):
            raise ValidationError(
                "A bulk-create body needs an assayResults list", details={"body_index": body_index}
            )
        if len(records) > BULK_CREATE_LIMIT:
            raise ValidationError(
                f"A bulk-create body holds at most {BULK_CREATE_LIMIT} results",
                details={"body_index": body_index, "records": len(records)},
            )
        table_id = body.get("tableId")
        if table_id is not None and (not isinstance(table_id, str) or not table_id.strip()):
            raise ValidationError(
                "tableId must be a results table ID string", details={"body_index": body_index}
            )
        for record_index, record in enumerate(records):
            where = {"body_index": body_index, "record_index": record_index}
            if not isinstance(record, dict):
                raise ValidationError("Each payload result must be an object", details=where)
            schema_id = record.get("schemaId")
            if not isinstance(schema_id, str) or not schema_id.strip():
                raise ValidationError("A payload result is missing schemaId", details=where)
            if not isinstance(record.get("fields"), dict):
                raise ValidationError("A payload result's fields must be an object", details=where)
            client_id = record.get("id")
            if client_id is not None and (not isinstance(client_id, str) or not client_id):
                raise ValidationError("A payload result's id must be a string", details=where)
            group = groups.setdefault(
                schema_id, {"schema_id": schema_id, "table_ids": [], "records": [], "indexes": []}
            )
            group["table_ids"].append(table_id)
            group["records"].append(
                {
                    "schemaId": schema_id,
                    "fields": record["fields"],
                    **({"id": client_id} if client_id is not None else {}),
                }
            )
            group["indexes"].append(row_index)
            row_index += 1
    if not row_index:
        raise ValidationError("The payload has no results")
    batches: list[dict[str, Any]] = []
    for group in groups.values():
        table_ids = set(group["table_ids"])
        table_id = table_ids.pop() if len(table_ids) == 1 else None
        batches.append(
            {
                "table_id": table_id,
                "schema_id": group["schema_id"],
                "table_name": table_id,
                "records": group["records"],
                "plan_row_offset": group["indexes"][0],
                "row_indexes": group["indexes"],
            }
        )
    return batches


def diff_payload_batch(
    batch: dict[str, Any], fetched: list[dict[str, Any]], *, missing_reason: str
) -> dict[str, list[dict[str, Any]]]:
    """``diff_batch``, after pairing each row that carries a client ``id`` with that record.

    A row whose ``id`` matches a fetched record's V2 or V3 id is diffed against that
    record alone. Every other row, and every unclaimed record, goes through one
    ``diff_batch`` call, which pairs by fields.
    """
    offset = batch.get("plan_row_offset", 0)
    offset = offset if isinstance(offset, int) and not isinstance(offset, bool) else 0
    raw_indexes = batch.get("row_indexes")
    records = list(batch.get("records") or [])
    indexes = (
        list(raw_indexes)
        if isinstance(raw_indexes, list) and len(raw_indexes) == len(records)
        else [offset + position for position in range(len(records))]
    )
    by_id = {
        key: position
        for position, record in enumerate(fetched)
        for key in (record.get("id"), record.get("v3Id"))
        if isinstance(key, str)
    }
    claimed: set[int] = set()
    parts: list[dict[str, list[dict[str, Any]]]] = []
    rest: list[Any] = []
    rest_indexes: list[int] = []
    for row, index in zip(records, indexes):
        client_id = row.get("id") if isinstance(row, dict) else None
        position = by_id.get(client_id) if isinstance(client_id, str) else None
        if position is None or position in claimed:
            rest.append(row)
            rest_indexes.append(index)
            continue
        claimed.add(position)
        single = {**batch, "records": [row], "row_indexes": [index]}
        parts.append(diff_batch(single, [fetched[position]], missing_reason=missing_reason))
    if not parts:
        return diff_batch(batch, fetched, missing_reason=missing_reason)
    unclaimed = [record for position, record in enumerate(fetched) if position not in claimed]
    remainder = {**batch, "records": rest, "row_indexes": rest_indexes}
    parts.append(diff_batch(remainder, unclaimed, missing_reason=missing_reason))
    merged: dict[str, list[dict[str, Any]]] = {}
    for key in ("matched", "mismatched", "missing", "extra"):
        rows = [row for part in parts for row in part[key]]
        if key != "extra":
            rows.sort(key=lambda row: int(row["plan_row_index"]))
        merged[key] = rows
    return merged
