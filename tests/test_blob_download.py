"""Blob and attachment metadata and verified file downloads."""

from __future__ import annotations

import hashlib
import json
import logging

import httpx
import pytest
from conftest import runtime_for
from typer.testing import CliRunner

from benchling_tools import cli
from benchling_tools.blob_download import download_blob
from benchling_tools.errors import ConflictError, NotFoundError, UpstreamError, ValidationError

BLOB = "00000000-0000-4000-8000-000000000001"
ATTACHMENT = "attachment_example"
DATA = b"synthetic download content\n"
SHA = hashlib.sha256(DATA).hexdigest()
MD5 = hashlib.md5(DATA, usedforsecurity=False).hexdigest()


@pytest.fixture
def files(fake):
    fake.blobs[BLOB] = {
        "id": BLOB,
        "name": "sample.txt",
        "type": "RAW_FILE",
        "mimeType": "text/plain",
        "uploadStatus": "COMPLETE",
        "data": DATA,
    }
    fake.attachments[ATTACHMENT] = {
        "id": ATTACHMENT,
        "name": "sample.txt",
        "mimeType": "text/plain",
        "uploadStatus": "SUCCEEDED",
        "downloadUrl": f"https://downloads.example.test/files/{ATTACHMENT}",
        "blobId": BLOB,
        "data": DATA,
    }


@pytest.mark.parametrize(
    ("identifier", "namespace"), [(BLOB, "v2_blob_uuid"), (ATTACHMENT, "v3_attachment")]
)
async def test_metadata_only(runtime, fake, files, identifier, namespace, tmp_path):
    before = set(tmp_path.iterdir())
    result = await runtime.service.get_blob(identifier)
    assert result["id_namespace"] == namespace
    assert "downloadUrl" not in result
    assert set(tmp_path.iterdir()) == before
    assert fake.download_headers == []
    assert fake.writes() == []


@pytest.mark.parametrize("identifier", [BLOB, ATTACHMENT])
async def test_verified_download(runtime, fake, files, identifier, tmp_path):
    output = tmp_path / "output"
    reference = tmp_path / "reference"
    reference.write_bytes(DATA)
    result = await download_blob(
        runtime.service,
        identifier,
        str(output),
        expect_sha256=SHA,
        expect_md5=MD5,
        expect_size=len(DATA),
        compare=str(reference),
    )
    assert output.read_bytes() == DATA
    assert (result["size"], result["sha256"], result["md5"], result["verified"]) == (
        len(DATA),
        SHA,
        MD5,
        True,
    )
    if identifier == ATTACHMENT:
        assert result["blob_id"] == BLOB
    assert "authorization" not in fake.download_headers[0]
    assert "cookie" not in fake.download_headers[0]
    assert fake.writes() == []


def test_cli_download_unverified_and_mismatches(benchling_env, fake, files, monkeypatch, tmp_path):
    monkeypatch.setattr(cli, "Runtime", lambda environment: runtime_for(fake, environment))
    runner = CliRunner()
    output = tmp_path / "output"
    args = ["blob", "download", BLOB, "--output", str(output), "--env", "sandbox"]
    for flags, code in [
        (["--expect-size", "1"], "size_mismatch"),
        (["--expect-sha256", "0" * 64], "checksum_mismatch"),
        (["--expect-md5", "0" * 32], "checksum_mismatch"),
    ]:
        result = runner.invoke(cli.app, args + flags)
        assert json.loads(result.stdout)["error"]["code"] == code
        assert not output.exists()
        assert not list(tmp_path.glob(".benchling-tools-*"))
    reference = tmp_path / "reference"
    reference.write_bytes(b"x" * len(DATA))
    result = runner.invoke(cli.app, args + ["--compare", str(reference)])
    assert json.loads(result.stdout)["error"]["code"] == "checksum_mismatch"
    assert not output.exists()
    result = runner.invoke(cli.app, args)
    payload = json.loads(result.stdout)
    assert result.exit_code == 0
    assert payload["data"]["verified"] is False
    assert "--compare" in payload["data"]["verification_hint"]
    assert output.read_bytes() == DATA
    result = runner.invoke(cli.app, args)
    assert json.loads(result.stdout)["error"]["code"] == "conflict"
    result = runner.invoke(cli.app, args + ["--force", "--expect-size", "1"])
    assert json.loads(result.stdout)["error"]["code"] == "size_mismatch"
    assert output.read_bytes() == DATA
    assert runner.invoke(cli.app, args + ["--force"]).exit_code == 0


async def test_invalid_and_missing_ids(runtime, files):
    with pytest.raises(ValidationError) as invalid:
        await runtime.service.get_blob("docpart_example")
    assert invalid.value.details["id_namespace"] == "unknown"
    assert "v3_attachment" in invalid.value.details["expected_id_namespace"]
    with pytest.raises(NotFoundError) as missing:
        await runtime.service.get_blob("attachment_missing")
    assert missing.value.details["id_namespace"] == "v3_attachment"
    assert missing.value.details["expected_id_namespace"] == "v3_attachment"


async def test_uppercase_uuid_and_download_logging(runtime, fake, files, tmp_path, caplog):
    blob = "abcdefab-cdef-4abc-8def-abcdefabcdef"
    fake.blobs[blob] = {**fake.blobs[BLOB], "id": blob}
    assert (await runtime.service.get_blob(blob.upper()))["id"] == blob
    with caplog.at_level(logging.DEBUG):
        result = await download_blob(runtime.service, blob.upper(), str(tmp_path / "output"))
    assert result["id"] == blob
    assert "downloads.example.test" not in caplog.text


async def test_unsafe_url_and_existing_file(runtime, fake, files, tmp_path):
    output = tmp_path / "output"
    fake.attachments[ATTACHMENT]["downloadUrl"] = "http://localhost/private"
    with pytest.raises(UpstreamError):
        await download_blob(runtime.service, ATTACHMENT, str(output))
    assert not output.exists()
    output.write_bytes(b"keep")
    with pytest.raises(ConflictError):
        await download_blob(runtime.service, ATTACHMENT, str(output))
    assert output.read_bytes() == b"keep"


@pytest.mark.parametrize(
    "options",
    [
        {"expect_sha256": "bad"},
        {"expect_md5": "bad"},
        {"expect_size": -1},
        {"expect_size": True},
        {"force": "yes"},
        {"compare": ""},
        {"compare": "/nonexistent/reference"},
    ],
)
async def test_invalid_download_options(runtime, files, tmp_path, options):
    with pytest.raises(ValidationError):
        await download_blob(runtime.service, BLOB, str(tmp_path / "output"), **options)


async def test_empty_output_and_missing_parent(runtime, files, tmp_path):
    with pytest.raises(ValidationError):
        await download_blob(runtime.service, BLOB, "")
    with pytest.raises(ValidationError):
        await download_blob(runtime.service, BLOB, str(tmp_path / "missing" / "output"))


@pytest.mark.parametrize(
    "url",
    [
        None,
        "https://127.0.0.1/file",
        "https://localhost./file",
        "https://downloads.example.test/file#fragment",
        "https://user@downloads.example.test/file",
    ],
)
async def test_invalid_download_url(runtime, fake, files, tmp_path, url):
    fake.attachments[ATTACHMENT]["downloadUrl"] = url
    with pytest.raises(UpstreamError):
        await download_blob(runtime.service, ATTACHMENT, str(tmp_path / "output"))
    assert not list(tmp_path.glob(".benchling-tools-*"))


@pytest.mark.parametrize("failure", ["status", "network"])
async def test_download_transport_failure(runtime, files, tmp_path, failure):
    def handler(request):
        if failure == "network":
            raise httpx.ConnectError("connection failed", request=request)
        return httpx.Response(403, request=request)

    runtime.api.download_transport = httpx.MockTransport(handler)
    with pytest.raises(UpstreamError):
        await download_blob(runtime.service, BLOB, str(tmp_path / "output"))
    assert not (tmp_path / "output").exists()
    assert not list(tmp_path.glob(".benchling-tools-*"))


async def test_output_race_and_invalid_metadata(runtime, fake, files, tmp_path, monkeypatch):
    def occupied(*args):
        raise FileExistsError

    monkeypatch.setattr("benchling_tools.blob_download.os.link", occupied)
    with pytest.raises(ConflictError):
        await download_blob(runtime.service, BLOB, str(tmp_path / "output"))
    assert not list(tmp_path.glob(".benchling-tools-*"))
    fake.attachments[ATTACHMENT]["id"] = "attachment_other"
    with pytest.raises(UpstreamError) as error:
        await runtime.service.get_blob(ATTACHMENT)
    assert error.value.details["id_namespace"] == "v3_attachment"
    with pytest.raises(ValidationError):
        await runtime.service.get_blob(BLOB.replace("-", ""))
