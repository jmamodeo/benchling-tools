"""Local files planned as Benchling blobs and linked into result attachment fields.

At plan time a ``{"file": PATH}`` value on an attachment field is hashed and
replaced with a ``{"local_file": {...}}`` descriptor that holds the base name,
size, SHA-256 and MD5 but not the path. The path is kept only in an owner-only
file under the state directory, so plans and previews never show it. At apply
the file is read again, and its size and SHA-256 must still match before any
upload. Uploads use ``POST /blobs`` up to ``SINGLE_UPLOAD_MAX_BYTES`` and the
multipart routes above it. An upload is never retried, and a multipart upload
that fails after it starts is aborted.
"""

from __future__ import annotations

import base64
import hashlib
import json
import mimetypes
import os
import re
import uuid
from copy import deepcopy
from pathlib import Path
from typing import Any
from urllib.parse import quote

from ..config import ConfigStore
from ..errors import (
    BenchlingAgentError,
    ConflictError,
    PartialCompletionError,
    ValidationError,
)
from ..models import ChangeJournal, ChangePlan
from ..services import field_definition_type, is_attachment_field
from .base import PlannerBase, write_event_details

MAX_FILE_BYTES = 100 * 1024 * 1024
MAX_PLAN_BYTES = 1024 * 1024 * 1024
# Benchling recommends multipart for blobs larger than 10MB. Each part except
# the last must be at least 5MB; around 10MB per part is recommended.
SINGLE_UPLOAD_MAX_BYTES = 10_000_000
PART_BYTES = 10 * 1024 * 1024
BLOB_TYPES = ("RAW_FILE", "VISUALIZATION")
_REFERENCE_KEYS = {"file", "blob_type", "mime_type"}
_DESCRIPTOR_KEYS = {"ref", "name", "size", "sha256", "md5", "mime_type", "blob_type"}
_REF = re.compile(r"[0-9a-f]{32}\Z")
_READ_CHUNK = 1024 * 1024

UPLOAD_WARNINGS = [
    "Local files are uploaded as blobs at apply, before the result write that links them",
    "Blob uploads are never retried. If a later step fails, an uploaded blob stays in "
    "Benchling without a result that links it",
    "The blob uploadStatus Benchling returns is recorded in the journal and is not polled",
]


def _mib(size: int) -> str:
    return f"{size / (1024 * 1024):.0f} MiB"


def is_file_reference(value: Any) -> bool:
    """True for a ``{"file": PATH}`` value that asks for a local upload."""
    return isinstance(value, dict) and "file" in value


def local_file_descriptor(value: Any) -> dict[str, Any] | None:
    """Return the inner descriptor of a planned upload, or None for any other value."""
    if not isinstance(value, dict) or set(value) != {"local_file"}:
        return None
    inner = value["local_file"]
    if not isinstance(inner, dict) or set(inner) != _DESCRIPTOR_KEYS:
        return None
    ref = inner.get("ref")
    if not isinstance(ref, str) or not _REF.fullmatch(ref):
        return None
    return inner


class FileStager:
    """Hashes local files for one plan and records where each one is."""

    def __init__(self, config: ConfigStore, base_dir: Path) -> None:
        self.config = config
        self.base_dir = base_dir
        self.uploads: list[dict[str, Any]] = []
        self.total_bytes = 0

    def stage(self, reference: Any, **context: Any) -> dict[str, Any]:
        """Replace a file reference with a descriptor; raises ValidationError if refused."""
        if not isinstance(reference, dict) or not isinstance(reference.get("file"), str):
            raise ValidationError('A local file reference must be {"file": PATH}')
        extra = sorted(set(reference) - _REFERENCE_KEYS)
        if extra:
            raise ValidationError(
                "A local file reference only accepts file, blob_type and mime_type",
                details={"unexpected": extra},
            )
        raw_path = reference["file"].strip()
        if not raw_path:
            raise ValidationError("A local file reference needs a non-empty file path")
        blob_type = reference.get("blob_type", "RAW_FILE")
        if blob_type not in BLOB_TYPES:
            raise ValidationError(
                "blob_type must be RAW_FILE or VISUALIZATION",
                details={"blob_type": blob_type},
            )
        path = Path(raw_path).expanduser()
        if not path.is_absolute():
            path = self.base_dir / path
        name = path.name
        mime_type = reference.get("mime_type")
        if mime_type is None:
            mime_type = mimetypes.guess_type(name)[0] or "application/octet-stream"
        if not isinstance(mime_type, str) or not mime_type or len(mime_type) > 100:
            raise ValidationError(
                "mime_type must be a non-empty string of at most 100 characters",
                details={"file": name},
            )
        if not name or len(name) > 1000:
            raise ValidationError(
                "A local file name must be 1 to 1000 characters", details={"file": name}
            )
        try:
            resolved = path.resolve(strict=True)
        except (OSError, RuntimeError) as exc:
            raise ValidationError(
                f"Local file {name} was not found", details={"file": name}
            ) from exc
        state_root = self.config.root.resolve()
        if resolved == state_root or state_root in resolved.parents:
            raise ValidationError(
                "Files inside the toolkit state directory cannot be uploaded",
                details={"file": name},
            )
        if not resolved.is_file():
            raise ValidationError(
                f"Local file {name} is not a regular file", details={"file": name}
            )
        size, sha256, md5 = _hash_file(resolved, name)
        if self.total_bytes + size > MAX_PLAN_BYTES:
            raise ValidationError(
                f"The files in one plan may total at most {MAX_PLAN_BYTES} bytes "
                f"({_mib(MAX_PLAN_BYTES)}). Split the upload into several plans",
                details={"file": name, "limit_bytes": MAX_PLAN_BYTES},
            )
        ref = uuid.uuid4().hex
        self.config.ensure()
        self.config.uploads_dir.mkdir(mode=0o700, exist_ok=True)
        try:
            self.config.uploads_dir.chmod(0o700)
        except OSError:
            pass
        _write_owner_only(
            self.config.uploads_dir / f"{ref}.json",
            {"path": str(resolved), "size": size, "sha256": sha256},
        )
        self.total_bytes += size
        descriptor = {
            "ref": ref,
            "name": name,
            "size": size,
            "sha256": sha256,
            "md5": md5,
            "mime_type": mime_type,
            "blob_type": blob_type,
        }
        self.uploads.append({**context, **descriptor})
        return {"local_file": descriptor}

    def discard(self) -> None:
        """Remove the recorded locations of every file staged by this plan."""
        discard_refs(self.config, [item["ref"] for item in self.uploads])


def _hash_file(path: Path, name: str) -> tuple[int, str, str]:
    try:
        size = path.stat().st_size
    except OSError as exc:
        raise ValidationError(f"Could not read local file {name}", details={"file": name}) from exc
    _check_size(size, name)
    sha256 = hashlib.sha256()
    md5 = hashlib.md5(usedforsecurity=False)
    read = 0
    try:
        with path.open("rb") as handle:
            while chunk := handle.read(_READ_CHUNK):
                read += len(chunk)
                _check_size(read, name)
                sha256.update(chunk)
                md5.update(chunk)
    except OSError as exc:
        raise ValidationError(f"Could not read local file {name}", details={"file": name}) from exc
    if read == 0:
        raise ValidationError(f"Local file {name} is empty", details={"file": name})
    return read, sha256.hexdigest(), md5.hexdigest()


def _check_size(size: int, name: str) -> None:
    if size > MAX_FILE_BYTES:
        raise ValidationError(
            f"Local file {name} is {size} bytes. The upload limit is {MAX_FILE_BYTES} bytes "
            f"({_mib(MAX_FILE_BYTES)}) per file",
            details={"file": name, "size": size, "limit_bytes": MAX_FILE_BYTES},
        )


def _write_owner_only(path: Path, payload: dict[str, Any]) -> None:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        json.dump(payload, handle)


def discard_refs(config: ConfigStore, refs: list[str]) -> None:
    """Delete recorded file locations; missing ones are ignored."""
    for ref in refs:
        if isinstance(ref, str) and _REF.fullmatch(ref):
            try:
                (config.uploads_dir / f"{ref}.json").unlink(missing_ok=True)
            except OSError:
                pass


def plan_upload_refs(plan: ChangePlan) -> set[str]:
    """Refs of the files a plan uploads, taken from its preview."""
    uploads = plan.after.get("uploads")
    if not isinstance(uploads, list):
        return set()
    return {
        item["ref"]
        for item in uploads
        if isinstance(item, dict) and isinstance(item.get("ref"), str)
    }


def stage_field_values(
    fields: dict[str, Any],
    definitions: list[dict[str, Any]],
    stager: FileStager,
    *,
    field_name_map: dict[str, str] | None = None,
    **context: Any,
) -> None:
    """Replace file references on attachment fields in place.

    A file reference on any other field, except a JSON field, is refused.
    """
    by_key: dict[str, dict[str, Any]] = {}
    for item in definitions:
        for key in (item.get("systemName"), item.get("id")):
            if isinstance(key, str) and key:
                by_key.setdefault(key, item)
    for key, field_input in fields.items():
        definition = by_key.get(key) or by_key.get((field_name_map or {}).get(key, ""))
        if definition is None or not isinstance(field_input, dict) or "value" not in field_input:
            continue
        value = field_input["value"]
        items = value if isinstance(value, list) else [value]
        field = str(definition.get("systemName") or key)
        if not is_attachment_field(definition):
            if field_definition_type(definition) != "JSON" and any(
                is_file_reference(item) for item in items
            ):
                raise ValidationError(
                    "Only attachment fields accept a local file reference",
                    details={"field": field},
                )
            continue
        staged = []
        for item in items:
            if isinstance(item, dict) and "local_file" in item:
                raise ValidationError(
                    'Pass a local file as {"file": PATH}, not as a planned descriptor',
                    details={"field": field},
                )
            staged.append(
                stager.stage(item, field=field, **context) if is_file_reference(item) else item
            )
        field_input["value"] = staged if isinstance(value, list) else staged[0]


class BlobUploads(PlannerBase):
    """Uploads planned local files and swaps their descriptors for blob IDs."""

    async def _link_local_files(
        self,
        fields: dict[str, Any],
        refs: set[str],
        journal: ChangeJournal,
        uploaded: dict[str, dict[str, Any]],
    ) -> dict[str, Any]:
        """Return a copy of ``fields`` with each planned descriptor replaced by a blob ID."""
        linked = deepcopy(fields)
        for field_input in linked.values():
            if not isinstance(field_input, dict) or "value" not in field_input:
                continue
            value = field_input["value"]
            items = value if isinstance(value, list) else [value]
            resolved = []
            for item in items:
                descriptor = local_file_descriptor(item)
                if descriptor is None or descriptor["ref"] not in refs:
                    resolved.append(item)
                    continue
                if descriptor["ref"] not in uploaded:
                    uploaded[descriptor["ref"]] = await self._upload_blob(descriptor, journal)
                resolved.append(uploaded[descriptor["ref"]]["blob_id"])
            field_input["value"] = resolved if isinstance(value, list) else resolved[0]
        return linked

    def _read_planned_file(self, descriptor: dict[str, Any]) -> bytes:
        name = descriptor["name"]
        stale = ConflictError(
            f"Local file {name} is missing or changed after the plan was created; "
            "create a new preview",
            details={"file": name},
        )
        location = self.repository.config.uploads_dir / f"{descriptor['ref']}.json"
        try:
            recorded = json.loads(location.read_text(encoding="utf-8"))
            path = Path(recorded["path"])
            if path.stat().st_size != descriptor["size"]:
                raise stale
            data = path.read_bytes()
        except (OSError, ValueError, KeyError, TypeError) as exc:
            raise stale from exc
        if (
            len(data) != descriptor["size"]
            or hashlib.sha256(data).hexdigest() != (descriptor["sha256"])
        ):
            raise stale
        return data

    async def _upload_blob(
        self, descriptor: dict[str, Any], journal: ChangeJournal
    ) -> dict[str, Any]:
        """Upload one planned file and journal its blob ID. Never retried."""
        data = self._read_planned_file(descriptor)
        summary = {
            "ref": descriptor["ref"],
            "name": descriptor["name"],
            "size": descriptor["size"],
            "sha256": descriptor["sha256"],
        }
        head = {
            "name": descriptor["name"],
            "type": descriptor["blob_type"],
            "mimeType": descriptor["mime_type"],
        }
        if len(data) <= SINGLE_UPLOAD_MAX_BYTES:
            try:
                response = await self.api.request(
                    "POST",
                    "/blobs",
                    version=2,
                    json={
                        **head,
                        "data64": base64.b64encode(data).decode("ascii"),
                        "md5": hashlib.md5(data, usedforsecurity=False).hexdigest(),
                    },
                )
            except BenchlingAgentError as exc:
                self._journal_upload_error(journal, summary, exc, method="single")
                raise
            return self._journal_upload(journal, summary, response, method="single", parts=1)
        try:
            started = await self.api.request(
                "POST", "/blobs:start-multipart-upload", version=2, json=head
            )
        except BenchlingAgentError as exc:
            self._journal_upload_error(journal, summary, exc, method="multipart")
            raise
        blob_id = started.get("id")
        if not isinstance(blob_id, str) or not blob_id:
            error = PartialCompletionError(
                "Benchling started a multipart blob upload but did not return a blob ID"
            )
            self._journal_upload_error(journal, summary, error, method="multipart")
            raise error
        self._event(
            journal,
            "upload_blob",
            "started",
            write_event_details(started, **summary, blob_id=blob_id, method="multipart"),
        )
        self.repository.save_journal(journal)
        quoted = quote(blob_id, safe="")
        try:
            parts: list[dict[str, Any]] = []
            for offset in range(0, len(data), PART_BYTES):
                chunk = data[offset : offset + PART_BYTES]
                number = len(parts) + 1
                part = await self.api.request(
                    "POST",
                    f"/blobs/{quoted}/parts",
                    version=2,
                    json={
                        "partNumber": number,
                        "data64": base64.b64encode(chunk).decode("ascii"),
                        "md5": hashlib.md5(chunk, usedforsecurity=False).hexdigest(),
                    },
                )
                etag = part.get("eTag")
                if not isinstance(etag, str) or not etag:
                    raise PartialCompletionError(
                        "Benchling accepted a blob part but did not return its eTag",
                        details={"blob_id": blob_id, "part_number": number},
                    )
                parts.append({"partNumber": number, "eTag": etag})
            completed = await self.api.request(
                "POST",
                f"/blobs/{quoted}:complete-upload",
                version=2,
                json={"parts": parts},
            )
        except BenchlingAgentError as exc:
            await self._abort_upload(journal, blob_id)
            self._journal_upload_error(
                journal, {**summary, "blob_id": blob_id}, exc, method="multipart"
            )
            raise
        return self._journal_upload(
            journal, summary, completed, method="multipart", parts=len(parts), blob_id=blob_id
        )

    async def _abort_upload(self, journal: ChangeJournal, blob_id: str) -> None:
        """Abort a started multipart upload once; the outcome is journaled, not raised."""
        try:
            response = await self.api.request(
                "POST", f"/blobs/{quote(blob_id, safe='')}:abort-upload", version=2
            )
        except BenchlingAgentError as exc:
            self._event(
                journal,
                "abort_blob_upload",
                "failed",
                {"blob_id": blob_id, "error": exc.message},
            )
        else:
            self._event(
                journal,
                "abort_blob_upload",
                "succeeded",
                write_event_details(response, blob_id=blob_id),
            )
        self.repository.save_journal(journal)

    def _journal_upload(
        self,
        journal: ChangeJournal,
        summary: dict[str, Any],
        response: dict[str, Any],
        *,
        method: str,
        parts: int,
        blob_id: str | None = None,
    ) -> dict[str, Any]:
        blob_id = response.get("id") or blob_id
        if not isinstance(blob_id, str) or not blob_id:
            error = PartialCompletionError(
                "Benchling accepted the blob upload but did not return a blob ID"
            )
            self._journal_upload_error(journal, summary, error, method=method)
            raise error
        uploaded = {
            **summary,
            "blob_id": blob_id,
            "method": method,
            "parts": parts,
            "upload_status": response.get("uploadStatus"),
        }
        self._event(journal, "upload_blob", "succeeded", write_event_details(response, **uploaded))
        self.repository.save_journal(journal)
        return uploaded

    def _journal_upload_error(
        self,
        journal: ChangeJournal,
        summary: dict[str, Any],
        exc: BenchlingAgentError,
        *,
        method: str,
    ) -> None:
        status = "unknown" if isinstance(exc, PartialCompletionError) else "failed"
        self._event(
            journal,
            "upload_blob",
            status,
            {**summary, "method": method, "error": exc.message},
        )
        self.repository.save_journal(journal)
