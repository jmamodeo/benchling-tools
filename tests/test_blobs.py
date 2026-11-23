"""Local files uploaded as blobs and linked into result attachment fields."""

from __future__ import annotations

import base64
import hashlib
import json
from pathlib import Path
from typing import Any

import pytest
from conftest import runtime_for
from fake_benchling import ENTRY_ID
from typer.testing import CliRunner

from benchling_tools import cli
from benchling_tools.config import ConfigStore
from benchling_tools.errors import ConflictError, PartialCompletionError, ValidationError
from benchling_tools.plans import blobs
from benchling_tools.services import is_attachment_field

PLOT = {
    "id": "assayfield_plot",
    "systemName": "plot",
    "name": "Plot",
    "__typename": "AttachmentLinkFieldDefinition",
    "derivationType": "DIRECT",
}
BLOB_1 = "00000000-0000-4000-8000-000000000001"


@pytest.fixture
def attachments(fake) -> None:
    fake.field_defs["assaysch_abc123"].append(dict(PLOT))


def _upload(fields: dict[str, Any]) -> dict[str, Any]:
    return {
        "table_id": "strtbl_example",
        "assay_results": [
            {"schemaId": "assaysch_abc123", "fields": {"ct": {"value": 12}, **fields}}
        ],
    }


def _write(path: Path, data: bytes) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


def _sidecars() -> list[Path]:
    directory = ConfigStore().uploads_dir
    return sorted(directory.iterdir()) if directory.exists() else []


def _blob_writes(fake) -> list[str]:
    return [call.path for call in fake.writes() if "/blobs" in call.path]


async def test_preview_hashes_the_file_and_never_shows_its_path(
    runtime, fake, tmp_path, attachments
) -> None:
    data = b"well,ct\nA1,12\n"
    path = _write(tmp_path / "private" / "run-01.csv", data)
    planned = await runtime.planner.plan(
        ENTRY_ID, "results", _upload({"plot": {"value": {"file": str(path)}}})
    )
    assert fake.writes() == []
    encoded = json.dumps(planned.model_dump(mode="json"))
    assert str(tmp_path) not in encoded
    assert "private" not in encoded
    assert data.decode() not in encoded
    [upload] = planned.after["uploads"]
    assert upload["name"] == "run-01.csv"
    assert upload["size"] == len(data)
    assert upload["sha256"] == hashlib.sha256(data).hexdigest()
    assert upload["md5"] == hashlib.md5(data, usedforsecurity=False).hexdigest()
    assert upload["mime_type"] == "text/csv"
    assert upload["blob_type"] == "RAW_FILE"
    assert upload["field"] == "plot" and upload["row_index"] == 0
    value = planned.after["records"][0]["fields"]["plot"]["value"]
    assert value["local_file"]["ref"] == upload["ref"]
    assert planned.request["assay_results"][0]["fields"]["plot"]["value"] == value
    assert any("never retried" in warning for warning in planned.warnings)
    [sidecar] = _sidecars()
    assert sidecar.stat().st_mode & 0o077 == 0


async def test_apply_uploads_once_then_links_the_blob_id(
    runtime, fake, tmp_path, attachments
) -> None:
    data = b"\x89PNG synthetic plot"
    path = _write(tmp_path / "plot.png", data)
    planned = await runtime.planner.plan(
        ENTRY_ID,
        "results",
        _upload({"plot": {"value": {"file": str(path), "blob_type": "VISUALIZATION"}}}),
    )
    applied = await runtime.planner.apply(planned.id)
    writes = fake.writes()
    assert [call.path for call in writes] == [
        "/api/v2/blobs",
        "/api/v2/assay-results:bulk-create",
    ]
    create = writes[0].body
    assert create["name"] == "plot.png"
    assert create["type"] == "VISUALIZATION"
    assert create["mimeType"] == "image/png"
    assert base64.b64decode(create["data64"]) == data
    assert create["md5"] == hashlib.md5(data, usedforsecurity=False).hexdigest()
    assert fake.bulk_payloads[0]["assayResults"][0]["fields"]["plot"] == {"value": BLOB_1}
    assert fake.blobs[BLOB_1]["data"] == data
    journal = applied["journal"]
    [event] = [item for item in journal["events"] if item["stage"] == "upload_blob"]
    assert event["status"] == "succeeded"
    assert event["details"]["blob_id"] == BLOB_1
    assert event["details"]["upload_status"] == "COMPLETE"
    assert event["details"]["method"] == "single"
    assert journal["result"]["blobs"][0]["blob_id"] == BLOB_1
    assert str(tmp_path) not in json.dumps(applied)
    assert _sidecars() == []


async def test_verify_compares_an_uploaded_file_by_its_blob_id(
    runtime, fake, tmp_path, attachments
) -> None:
    path = _write(tmp_path / "plot.png", b"synthetic plot")
    planned = await runtime.planner.plan(
        ENTRY_ID, "results", _upload({"plot": {"value": {"file": str(path)}}})
    )
    await runtime.planner.apply(planned.id)
    fake.complete_upload()

    data = await runtime.planner.verify_results(planned.id)
    assert data["counts"]["matched"] == 1
    assert data["mismatched"] == data["missing"] == []

    fake.assay_results[0]["fields"]["plot"]["value"] = "00000000-0000-4000-8000-000000000099"
    data = await runtime.planner.verify_results(planned.id)
    [row] = data["mismatched"]
    assert row["fields"] == [
        {
            "field": "plot",
            "planned": BLOB_1,
            "actual": "00000000-0000-4000-8000-000000000099",
        }
    ]


async def test_multi_value_attachment_mixes_files_and_existing_blob_ids(
    runtime, fake, tmp_path
) -> None:
    fake.field_defs["assaysch_abc123"].append({**PLOT, "isMulti": True})
    first = _write(tmp_path / "a.txt", b"first")
    second = _write(tmp_path / "b.txt", b"second")
    existing = "11111111-1111-4111-8111-111111111111"
    planned = await runtime.planner.plan(
        ENTRY_ID,
        "results",
        _upload({"plot": {"value": [{"file": str(first)}, existing, {"file": str(second)}]}}),
    )
    assert [item["name"] for item in planned.after["uploads"]] == ["a.txt", "b.txt"]
    await runtime.planner.apply(planned.id)
    sent = fake.bulk_payloads[0]["assayResults"][0]["fields"]["plot"]["value"]
    assert sent == [BLOB_1, existing, "00000000-0000-4000-8000-000000000002"]


async def test_large_file_uses_multipart_upload(
    runtime, fake, tmp_path, attachments, monkeypatch
) -> None:
    monkeypatch.setattr(blobs, "SINGLE_UPLOAD_MAX_BYTES", 10)
    monkeypatch.setattr(blobs, "PART_BYTES", 8)
    data = bytes(range(20))
    path = _write(tmp_path / "raw.bin", data)
    planned = await runtime.planner.plan(
        ENTRY_ID, "results", _upload({"plot": {"value": {"file": str(path)}}})
    )
    applied = await runtime.planner.apply(planned.id)
    assert _blob_writes(fake) == [
        "/api/v2/blobs:start-multipart-upload",
        f"/api/v2/blobs/{BLOB_1}/parts",
        f"/api/v2/blobs/{BLOB_1}/parts",
        f"/api/v2/blobs/{BLOB_1}/parts",
        f"/api/v2/blobs/{BLOB_1}:complete-upload",
    ]
    parts = [call.body for call in fake.writes() if call.path.endswith("/parts")]
    assert [part["partNumber"] for part in parts] == [1, 2, 3]
    assert [len(base64.b64decode(part["data64"])) for part in parts] == [8, 8, 4]
    complete = fake.writes()[4].body
    assert [part["partNumber"] for part in complete["parts"]] == [1, 2, 3]
    assert fake.blobs[BLOB_1]["data"] == data
    assert fake.blobs[BLOB_1]["uploadStatus"] == "COMPLETE"
    assert fake.bulk_payloads[0]["assayResults"][0]["fields"]["plot"] == {"value": BLOB_1}
    stages = [(item["stage"], item["status"]) for item in applied["journal"]["events"]]
    assert ("upload_blob", "started") in stages
    assert ("upload_blob", "succeeded") in stages


async def test_failed_part_aborts_the_upload_and_writes_no_result(
    runtime, fake, tmp_path, attachments, monkeypatch
) -> None:
    monkeypatch.setattr(blobs, "SINGLE_UPLOAD_MAX_BYTES", 10)
    monkeypatch.setattr(blobs, "PART_BYTES", 8)
    path = _write(tmp_path / "raw.bin", bytes(20))
    planned = await runtime.planner.plan(
        ENTRY_ID, "results", _upload({"plot": {"value": {"file": str(path)}}})
    )
    fake.fail("POST", "/parts", 500, skip=1)
    with pytest.raises(PartialCompletionError) as raised:
        await runtime.planner.apply(planned.id)
    paths = [call.path for call in fake.writes()]
    assert paths[-1] == f"/api/v2/blobs/{BLOB_1}:abort-upload"
    assert paths.count(f"/api/v2/blobs/{BLOB_1}/parts") == 2
    assert not any("bulk-create" in item for item in paths)
    assert fake.blobs[BLOB_1]["uploadStatus"] == "ABORTED"
    journal = raised.value.details["journal"]
    stages = [(item["stage"], item["status"]) for item in journal["events"]]
    assert ("abort_blob_upload", "succeeded") in stages
    assert ("upload_blob", "unknown") in stages
    assert ("create_results", "unknown") in stages
    assert _sidecars() == []


async def test_rejected_single_upload_fails_without_retry_or_result(
    runtime, fake, tmp_path, attachments
) -> None:
    path = _write(tmp_path / "raw.bin", b"data")
    planned = await runtime.planner.plan(
        ENTRY_ID, "results", _upload({"plot": {"value": {"file": str(path)}}})
    )
    fake.fail("POST", "/api/v2/blobs", 400)
    with pytest.raises(ValidationError):
        await runtime.planner.apply(planned.id)
    assert [call.path for call in fake.writes()] == ["/api/v2/blobs"]
    journal = runtime.planner.repository.load_journal(planned.id)
    assert journal.status.value == "failed"
    assert [(e.stage, e.status) for e in journal.events if e.stage == "upload_blob"] == [
        ("upload_blob", "failed")
    ]


async def test_file_over_the_size_cap_is_refused(
    runtime, fake, tmp_path, attachments, monkeypatch
) -> None:
    monkeypatch.setattr(blobs, "MAX_FILE_BYTES", 4)
    path = _write(tmp_path / "big.bin", b"12345")
    with pytest.raises(ValidationError) as raised:
        await runtime.planner.plan(
            ENTRY_ID, "results", _upload({"plot": {"value": {"file": str(path)}}})
        )
    assert "big.bin is 5 bytes. The upload limit is 4 bytes" in raised.value.message
    assert str(tmp_path) not in json.dumps(raised.value.details)
    assert fake.writes() == []
    assert _sidecars() == []


async def test_files_over_the_plan_total_are_refused_and_staging_is_undone(
    runtime, fake, tmp_path, monkeypatch
) -> None:
    fake.field_defs["assaysch_abc123"].append({**PLOT, "isMulti": True})
    monkeypatch.setattr(blobs, "MAX_PLAN_BYTES", 5)
    first = _write(tmp_path / "a.bin", b"123")
    second = _write(tmp_path / "b.bin", b"456")
    with pytest.raises(ValidationError) as raised:
        await runtime.planner.plan(
            ENTRY_ID,
            "results",
            _upload({"plot": {"value": [{"file": str(first)}, {"file": str(second)}]}}),
        )
    assert "total at most 5 bytes" in raised.value.message
    assert _sidecars() == []


async def test_file_changed_after_preview_is_a_conflict(
    runtime, fake, tmp_path, attachments
) -> None:
    path = _write(tmp_path / "raw.bin", b"before")
    planned = await runtime.planner.plan(
        ENTRY_ID, "results", _upload({"plot": {"value": {"file": str(path)}}})
    )
    path.write_bytes(b"after!")
    with pytest.raises(ConflictError) as raised:
        await runtime.planner.apply(planned.id)
    assert "raw.bin is missing or changed" in raised.value.message
    assert fake.writes() == []
    assert _sidecars() == []


@pytest.mark.parametrize(
    ("value", "message"),
    [
        ({"file": "missing.bin"}, "Local file missing.bin was not found"),
        ({"file": "raw.bin", "extra": 1}, "only accepts file, blob_type and mime_type"),
        ({"file": "raw.bin", "blob_type": "IMAGE"}, "blob_type must be RAW_FILE or VISUALIZATION"),
        ({"file": "empty.bin"}, "Local file empty.bin is empty"),
        ({"file": "."}, "is not a regular file"),
    ],
)
async def test_unusable_file_references_are_refused(
    runtime, fake, tmp_path, attachments, monkeypatch, value, message
) -> None:
    monkeypatch.chdir(tmp_path)
    _write(tmp_path / "raw.bin", b"data")
    _write(tmp_path / "empty.bin", b"")
    with pytest.raises(ValidationError) as raised:
        await runtime.planner.plan(ENTRY_ID, "results", _upload({"plot": {"value": value}}))
    assert message in json.dumps(raised.value.details) + raised.value.message
    assert str(tmp_path) not in json.dumps(raised.value.details)
    assert fake.writes() == []
    assert _sidecars() == []


async def test_files_in_the_state_directory_are_refused(runtime, fake, attachments) -> None:
    config = ConfigStore()
    config.ensure()
    secret = _write(config.root / "environments.json", b"{}")
    with pytest.raises(ValidationError) as raised:
        await runtime.planner.plan(
            ENTRY_ID, "results", _upload({"plot": {"value": {"file": str(secret)}}})
        )
    assert "state directory" in json.dumps(raised.value.details)


async def test_file_reference_needs_an_attachment_field(
    runtime, fake, tmp_path, attachments
) -> None:
    path = _write(tmp_path / "raw.bin", b"data")
    with pytest.raises(ValidationError) as raised:
        await runtime.planner.plan(
            ENTRY_ID, "results", _upload({"note": {"value": {"file": str(path)}}})
        )
    assert "Only attachment fields accept a local file" in json.dumps(raised.value.details)
    assert _sidecars() == []


async def test_a_supplied_descriptor_is_not_trusted(runtime, fake, attachments) -> None:
    forged = {
        "local_file": {
            "ref": "0" * 32,
            "name": "x",
            "size": 1,
            "sha256": "0",
            "md5": "0",
            "mime_type": "text/plain",
            "blob_type": "RAW_FILE",
        }
    }
    with pytest.raises(ValidationError) as raised:
        await runtime.planner.plan(ENTRY_ID, "results", _upload({"plot": {"value": forged}}))
    assert "not as a planned descriptor" in json.dumps(raised.value.details)


async def test_entry_fields_do_not_accept_descriptors(runtime, fake) -> None:
    planner = runtime.planner
    descriptor = {
        "local_file": {
            "ref": "0" * 32,
            "name": "x",
            "size": 1,
            "sha256": "0",
            "md5": "0",
            "mime_type": "text/plain",
            "blob_type": "RAW_FILE",
        }
    }
    with pytest.raises(ValidationError):
        planner._validate_schema_field_value(PLOT, descriptor)
    planner._validate_schema_field_value(PLOT, descriptor, local_files=True)
    planner._validate_schema_field_value(PLOT, BLOB_1)


def test_attachment_fields_are_recognized() -> None:
    assert is_attachment_field(PLOT)
    assert is_attachment_field({"type": "blob_link"})
    assert is_attachment_field({"type": "ATTACHMENT_LINK"})
    assert not is_attachment_field({"__typename": "CustomEntityLinkFieldDefinition"})
    assert not is_attachment_field({"__typename": "BlobFieldDefinition"})


async def test_csv_update_uploads_a_file_cell_and_patches_the_blob_id(
    runtime, fake, tmp_path, attachments
) -> None:
    fake.results = [
        {
            "id": "result_1",
            "modifiedAt": "2026-01-02T00:00:00Z",
            "entry": {"id": ENTRY_ID},
            "schema": {"id": "assaysch_abc123"},
            "schemaFields": [],
        }
    ]
    data = b"trace"
    _write(tmp_path / "plots" / "trace.bin", data)
    csv_path = tmp_path / "update.csv"
    csv_path.write_text("id,Plot\nresult_1,file:plots/trace.bin\n", encoding="utf-8")
    planned = await runtime.planner.plan(
        ENTRY_ID,
        "results",
        {
            "operation": "update",
            "csv_path": str(csv_path),
            "id_column": "id",
            "column_map": {"Plot": "Plot"},
        },
    )
    assert fake.writes() == []
    [upload] = planned.after["uploads"]
    assert upload["name"] == "trace.bin" and upload["result_id"] == "result_1"
    assert "plots" not in json.dumps(planned.after)
    applied = await runtime.planner.apply(planned.id)
    assert [call.path for call in fake.writes()] == ["/api/v2/blobs", "/api/v3/result/result_1"]
    assert fake.result_updates[0]["body"] == {
        "schemaFields": {"assayfield_plot": {"value": BLOB_1}}
    }
    assert applied["journal"]["result"]["blobs"][0]["blob_id"] == BLOB_1
    assert _sidecars() == []


async def test_get_blob_reads_metadata_only(runtime, fake) -> None:
    fake.blobs[BLOB_1] = {
        "id": BLOB_1,
        "name": "plot.png",
        "type": "VISUALIZATION",
        "mimeType": "image/png",
        "uploadStatus": "COMPLETE",
        "data": b"x",
        "parts": {},
    }
    blob = await runtime.service.get_blob(BLOB_1)
    assert dict(blob) == {
        "id": BLOB_1,
        "name": "plot.png",
        "type": "VISUALIZATION",
        "mimeType": "image/png",
        "uploadStatus": "COMPLETE",
        "id_namespace": "v2_blob_uuid",
    }
    assert fake.writes() == []
    with pytest.raises(ValidationError):
        await runtime.service.get_blob(" ")


def test_cli_blob_get(benchling_env, fake, monkeypatch) -> None:
    monkeypatch.setattr(cli, "Runtime", lambda environment: runtime_for(fake, environment))
    fake.blobs[BLOB_1] = {
        "id": BLOB_1,
        "name": "plot.png",
        "type": "RAW_FILE",
        "mimeType": "image/png",
        "uploadStatus": "COMPLETE",
    }
    result = CliRunner().invoke(cli.app, ["blob", "get", BLOB_1, "--env", "sandbox"])
    payload = json.loads(result.stdout)
    assert result.exit_code == 0
    assert payload["data"]["name"] == "plot.png"
    assert fake.writes() == []
