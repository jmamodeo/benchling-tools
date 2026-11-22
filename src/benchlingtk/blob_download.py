"""Stream and verify blob downloads before publishing a local file."""

from __future__ import annotations

import hashlib
import ipaddress
import os
import re
import tempfile
from pathlib import Path
from typing import Any
from urllib.parse import quote

import httpx

from .errors import (
    ChecksumMismatchError,
    ConflictError,
    SizeMismatchError,
    UpstreamError,
    ValidationError,
)
from .services import BenchlingService
from .tls import verified_context


def _reference(path: str) -> tuple[int, str, str]:
    sha256 = hashlib.sha256()
    md5 = hashlib.md5(usedforsecurity=False)
    size = 0
    try:
        with open(path, "rb") as source:
            while chunk := source.read(1024 * 1024):
                size += len(chunk)
                sha256.update(chunk)
                md5.update(chunk)
    except OSError as exc:
        raise ValidationError("Cannot read the comparison file", details={"path": path}) from exc
    return size, sha256.hexdigest(), md5.hexdigest()


def _download_url(value: Any) -> str:
    if not isinstance(value, str):
        raise UpstreamError("Benchling did not provide a download URL")
    try:
        url = httpx.URL(value)
        if (
            url.scheme != "https"
            or not url.host
            or url.username
            or url.password
            or url.fragment
            or url.host.lower().rstrip(".") == "localhost"
            or url.host.lower().rstrip(".").endswith(".localhost")
        ):
            raise ValueError
        try:
            address = ipaddress.ip_address(url.host)
        except ValueError:
            address = None
        if address is not None and not address.is_global:
            raise ValueError
    except (ValueError, httpx.InvalidURL) as exc:
        raise UpstreamError("Benchling returned an unsafe download URL") from exc
    return value


async def download_blob(
    service: BenchlingService,
    identifier: str,
    output: str,
    *,
    expect_sha256: str | None = None,
    expect_md5: str | None = None,
    expect_size: int | None = None,
    compare: str | None = None,
    force: bool = False,
) -> dict[str, Any]:
    """Download to a temporary file and publish only after all supplied checks pass."""
    for label, digest, length in (("sha256", expect_sha256, 64), ("md5", expect_md5, 32)):
        if digest is not None and (
            not isinstance(digest, str) or not re.fullmatch(rf"[0-9a-fA-F]{{{length}}}", digest)
        ):
            raise ValidationError(f"Expected {label} must be {length} hexadecimal characters")
    if expect_size is not None and (
        isinstance(expect_size, bool) or not isinstance(expect_size, int) or expect_size < 0
    ):
        raise ValidationError("Expected size must be a non-negative integer")
    if not isinstance(output, str) or not output.strip():
        raise ValidationError("Output path is required")
    if not isinstance(force, bool):
        raise ValidationError("force must be true or false")
    if compare is not None and (not isinstance(compare, str) or not compare.strip()):
        raise ValidationError("Comparison path must be a non-empty string")
    reference = _reference(compare) if compare is not None else None
    destination = Path(output)
    if not force and os.path.lexists(destination):
        raise ConflictError("Output file already exists", details={"path": output})

    blob_id, namespace, metadata = await service._blob_metadata(identifier)
    if namespace == "v3_attachment":
        url = metadata.get("downloadUrl")
    else:
        url_response = await service.api.request(
            "GET", f"/blobs/{quote(blob_id, safe='')}/download-url", version=2
        )
        url = url_response.get("downloadURL")
    url = _download_url(url)

    temp_path: str | None = None
    size = 0
    sha256 = hashlib.sha256()
    md5 = hashlib.md5(usedforsecurity=False)
    try:
        with tempfile.NamedTemporaryFile(
            dir=destination.parent, prefix=".benchlingtk-", delete=False
        ) as temp:
            temp_path = temp.name
            request = httpx.Request(
                "GET",
                url,
                headers={"Accept-Encoding": "identity"},
                extensions={"timeout": dict.fromkeys(("connect", "read", "write", "pool"), 30.0)},
            )
            transport = service.api.download_transport or httpx.AsyncHTTPTransport(
                verify=verified_context(), retries=0
            )
            try:
                response = await transport.handle_async_request(request)
                try:
                    if response.status_code != 200:
                        raise UpstreamError(
                            "Blob download failed", details={"status": response.status_code}
                        )
                    async for chunk in response.aiter_raw():
                        temp.write(chunk)
                        size += len(chunk)
                        sha256.update(chunk)
                        md5.update(chunk)
                finally:
                    await response.aclose()
            except httpx.HTTPError as exc:
                raise UpstreamError("Blob download failed", retryable=True) from exc
            finally:
                if service.api.download_transport is None:
                    await transport.aclose()
        sha = sha256.hexdigest()
        md = md5.hexdigest()
        sizes = [
            item for item in (expect_size, reference[0] if reference else None) if item is not None
        ]
        if any(size != item for item in sizes):
            raise SizeMismatchError("Downloaded size does not match the expected size")
        for actual, expected, ref in (
            (sha, expect_sha256, reference[1] if reference else None),
            (md, expect_md5, reference[2] if reference else None),
        ):
            if any(actual != item.lower() for item in (expected, ref) if item is not None):
                raise ChecksumMismatchError(
                    "Downloaded checksum does not match the expected checksum"
                )
        if force:
            os.replace(temp_path, destination)
        else:
            try:
                os.link(temp_path, destination)
            except FileExistsError as exc:
                raise ConflictError("Output file already exists", details={"path": output}) from exc
            os.unlink(temp_path)
        temp_path = None
    except OSError as exc:
        raise ValidationError("Cannot write the output file", details={"path": output}) from exc
    finally:
        if temp_path is not None:
            try:
                os.unlink(temp_path)
            except FileNotFoundError:
                pass

    summary: dict[str, Any] = {
        "id": blob_id,
        "id_namespace": namespace,
        "path": output,
        "size": size,
        "sha256": sha,
        "md5": md,
        "verified": any(
            value is not None for value in (expect_sha256, expect_md5, expect_size, compare)
        ),
    }
    if isinstance(metadata.get("blobId"), str):
        summary["blob_id"] = metadata["blobId"]
    if not summary["verified"]:
        summary["verification_hint"] = (
            "Use --expect-sha256, --expect-md5, --expect-size or --compare."
        )
    return summary
