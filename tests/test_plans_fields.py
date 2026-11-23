from __future__ import annotations

import pytest
from fake_benchling import ENTRY_ID

from benchling_tools.errors import ConflictError, UnsupportedError, ValidationError


async def plan(runtime, fields):
    return await runtime.planner.plan(ENTRY_ID, "fields", {"schema_fields": fields})


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("temperature", 0),
        ("temperature", 100),
        ("notes", "free text"),
        ("notes", None),
        ("ratio", 0.5),
        ("ratio", 3),
        ("flag", False),
        ("run_date", "2026-01-31"),
        ("started_at", "2026-01-31T10:00:00Z"),
        ("started_at", "2026-01-31T10:00:00-05:00"),
        ("tags", ["a", "b"]),
        ("tags", []),
        ("meta", {"anything": [1, 2]}),
        ("sample_link", "etr_abc123"),
    ],
)
async def test_valid_field_values_are_planned(runtime, fake, key, value) -> None:
    planned = await plan(runtime, {key: {"value": value}})
    target = planned.after["targets"][0]
    assert target["system_name"] == key
    assert planned.request["schema_fields"] == {target["field_definition_id"]: {"value": value}}
    assert planned.after["fields"][key]["field_definition_id"] == target["field_definition_id"]
    assert planned.after["fields"][key]["system_name"] == key
    assert planned.before["fields"][key]["field_definition_id"] == target["field_definition_id"]
    assert planned.after["fields"][key]["value"] == {"value": value}
    assert fake.writes() == []


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("temperature", "5"),
        ("temperature", 5.5),
        ("temperature", True),
        ("temperature", -1),
        ("temperature", 101),
        ("temperature", None),
        ("notes", 5),
        ("ratio", 0.4),
        ("ratio", True),
        ("ratio", "1.0"),
        ("flag", "yes"),
        ("flag", 1),
        ("run_date", "2026-1-31"),
        ("run_date", "01/31/2026"),
        ("run_date", "2026-01-31T00:00:00Z"),
        ("run_date", 20260131),
        ("started_at", "2026-01-31T10:00:00"),
        ("started_at", "yesterday"),
        ("started_at", 5),
        ("tags", "a"),
        ("tags", [1]),
        ("sample_link", ""),
        ("sample_link", 7),
    ],
)
async def test_invalid_field_values_are_rejected(runtime, fake, key, value) -> None:
    with pytest.raises(ValidationError) as caught:
        await plan(runtime, {key: {"value": value}})
    assert caught.value.details["field"] == key


@pytest.mark.parametrize("key", ["weird", "calculated", "old"])
async def test_unsupported_or_read_only_fields_cannot_be_planned(runtime, key) -> None:
    with pytest.raises(UnsupportedError) as caught:
        await plan(runtime, {key: {"value": 1}})
    assert caught.value.details["field"] == key


async def test_system_names_and_ids_both_resolve_to_field_definition_ids(runtime, fake) -> None:
    planned = await plan(runtime, {"tsf_notes": {"value": "x"}, "temperature": {"value": 30}})
    assert set(planned.request["schema_fields"]) == {"tsf_notes", "tsf_temp"}
    assert planned.after["fields"]["notes"]["system_name"] == "notes"
    assert planned.after["fields"]["notes"]["field_definition_id"] == "tsf_notes"
    assert planned.after["fields"]["temperature"]["value"] == {"value": 30}
    assert planned.before["fields"]["temperature"]["value"] == 20


async def test_unknown_fields_list_available_addresses(runtime) -> None:
    with pytest.raises(ValidationError) as caught:
        await plan(runtime, {"nonexistent": {"value": 1}, "Temperature": {"value": 1}})
    details = caught.value.details
    assert details["unknown_fields"] == ["nonexistent", "Temperature"]
    assert "temperature" in details["available_system_names"]
    assert "tsf_temp" in details["available_field_ids"]


async def test_entry_field_unknown_errors_omit_display_name_hints(runtime) -> None:
    # Entry field updates address fields by system name or ID only, so the
    # error must not suggest display names that would be rejected.
    with pytest.raises(ValidationError) as caught:
        await plan(runtime, {"Temperature": {"value": 1}})
    details = caught.value.details
    assert details["unknown_fields"] == ["Temperature"]
    assert "available_display_names" not in details


@pytest.mark.parametrize(
    "fields",
    [
        {"temperature": {"value": 1, "extra": 2}},
        {"temperature": {}},
        {"temperature": 5},
        {"temperature": {"value": 1}, "tsf_temp": {"value": 2}},
    ],
)
async def test_malformed_or_duplicate_field_inputs_are_rejected(runtime, fields) -> None:
    with pytest.raises(ValidationError):
        await plan(runtime, fields)


@pytest.mark.parametrize("fields", [{}, []])
async def test_empty_field_plan_is_rejected(runtime, fields) -> None:
    with pytest.raises(ValidationError, match="non-empty"):
        await runtime.planner.plan(ENTRY_ID, "fields", {"schema_fields": fields})


async def test_entry_without_schema_is_unsupported(runtime, fake) -> None:
    fake.entry_schema_id = None
    with pytest.raises(UnsupportedError, match="no assigned schema"):
        await plan(runtime, {"temperature": {"value": 1}})


async def test_field_without_an_id_cannot_be_written(runtime, fake) -> None:
    fake.field_defs["entsch_abc123"].append(
        {
            "systemName": "noid",
            "name": "No id",
            "type": "text",
            "derivationType": "DIRECT",
        }
    )
    with pytest.raises(UnsupportedError, match="field-definition ID"):
        await plan(runtime, {"noid": {"value": "x"}})


async def test_schema_without_addressable_fields_is_unsupported(runtime, fake) -> None:
    fake.field_defs["entsch_abc123"] = []
    with pytest.raises(UnsupportedError, match="addressable"):
        await plan(runtime, {"temperature": {"value": 1}})


async def test_apply_sends_normalized_schema_fields_and_updates_entry(runtime, fake) -> None:
    planned = await plan(runtime, {"tsf_temp": {"value": 42}})
    applied = await runtime.planner.apply(planned.id)
    assert fake.entry_patches == [{"schemaFields": {"tsf_temp": {"value": 42}}}]
    assert fake.entry_fields["Temperature"] == 42
    assert applied["journal"]["result"]["concurrency"] == "best_effort_value_comparison"
    assert applied["journal"]["status"] == "succeeded"
    updated = next(
        event for event in applied["journal"]["events"] if event["stage"] == "update_fields"
    )
    assert updated["details"]["http_status"] == 200


async def test_apply_detects_changed_field_definitions(runtime, fake) -> None:
    planned = await plan(runtime, {"temperature": {"value": 42}})
    next(d for d in fake.field_defs["entsch_abc123"] if d["id"] == "tsf_temp")["numericMax"] = 50
    with pytest.raises(ConflictError, match="field definitions"):
        await runtime.planner.apply(planned.id)
    assert fake.writes() == []


async def test_apply_detects_schema_change(runtime, fake) -> None:
    planned = await plan(runtime, {"temperature": {"value": 42}})
    fake.field_defs["entsch_other"] = fake.field_defs["entsch_abc123"]
    fake.entry_schema_id = "entsch_other"
    with pytest.raises(ConflictError, match="schema"):
        await runtime.planner.apply(planned.id)
    assert fake.writes() == []


async def test_apply_detects_value_drift_and_journals_failure(runtime, fake) -> None:
    planned = await plan(runtime, {"temperature": {"value": 42}})
    fake.entry_fields["Temperature"] = 19
    with pytest.raises(ConflictError) as caught:
        await runtime.planner.apply(planned.id)
    assert caught.value.details["actual"] == {"temperature": 19}
    assert fake.writes() == []
    assert (await runtime.planner.status(planned.id))["journal"]["status"] == "failed"
