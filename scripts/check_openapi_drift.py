#!/usr/bin/env python3
"""Compare the public V3 OpenAPI spec with contracts/openapi-manifest.json.

Reads the V3 contract URL, checksum, snapshot, and ``routes_used`` list from
the manifest. Downloads that URL (or reads ``--spec-file``) and exits 1 when
the SHA-256 differs. The failure names each recorded route whose path item or
referenced definition changed.

    uv run python scripts/check_openapi_drift.py
    uv run python scripts/check_openapi_drift.py --spec-file path/to/openapi.yaml
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import urllib.error
import urllib.request
from datetime import date, datetime
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parent.parent
V3_CONTRACT = "benchling-v3"
MAX_SPEC_BYTES = 32 * 1024 * 1024
MAX_REF_DETAILS = 8
_SHA256 = re.compile(r"^[0-9a-fA-F]{64}$")
_PARAM = re.compile(r"^\{[^{}/]+\}$")
_USER_AGENT = "benchling-tools-openapi-drift"


class DriftCheckError(Exception):
    """The drift check could not compare the public spec with the manifest."""


def load_manifest(path: Path) -> dict[str, Any]:
    """Return the parsed manifest object."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise DriftCheckError(f"Could not read {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise DriftCheckError(f"{path} is not a JSON object.")
    return data


def v3_contract(manifest: dict[str, Any]) -> dict[str, Any]:
    """Return the single public V3 contract entry."""
    contracts = manifest.get("contracts")
    if not isinstance(contracts, list):
        raise DriftCheckError("Manifest has no contracts list.")
    matches = [
        item for item in contracts if isinstance(item, dict) and item.get("name") == V3_CONTRACT
    ]
    if len(matches) != 1:
        raise DriftCheckError(f"Manifest must contain exactly one {V3_CONTRACT} contract.")
    contract = matches[0]
    url = contract.get("url")
    if not isinstance(url, str) or not url.startswith("https://"):
        raise DriftCheckError(f"{V3_CONTRACT} url must be an https URL.")
    digest = contract.get("sha256")
    if not isinstance(digest, str) or _SHA256.fullmatch(digest) is None:
        raise DriftCheckError(f"{V3_CONTRACT} sha256 must be a 64-character hex digest.")
    snapshot = contract.get("snapshot")
    if not isinstance(snapshot, str) or not snapshot:
        raise DriftCheckError(f"{V3_CONTRACT} snapshot must be a path.")
    relative = Path(snapshot)
    if relative.is_absolute() or any(part == ".." for part in relative.parts):
        raise DriftCheckError(f"{V3_CONTRACT} snapshot must be a path inside the repository.")
    routes = contract.get("routes_used")
    if not isinstance(routes, list) or not routes:
        raise DriftCheckError(f"{V3_CONTRACT} routes_used must list the paths the toolkit uses.")
    if not all(isinstance(route, str) and route.startswith("/") for route in routes):
        raise DriftCheckError(f"{V3_CONTRACT} routes_used must be a list of absolute paths.")
    return contract


def route_key(path: str) -> tuple[str, ...]:
    """Normalize a path so parameter names do not affect the match.

    ``/document-part-unstructured-table/{doc_part_id}`` and the public template
    ``/document-part-unstructured-table/{document_part_unstructured_table_id}``
    share a key. A suffix glued to a parameter, such as
    ``{result_schema_id}:set-field-definitions``, stays distinct.
    """
    segments = [segment for segment in path.split("/") if segment]
    if not segments:
        raise DriftCheckError(f"Route {path!r} has no path segments.")
    return tuple("{param}" if _PARAM.fullmatch(segment) else segment for segment in segments)


def parse_spec(text: str, label: str) -> dict[str, Any]:
    """Parse an OpenAPI YAML document."""
    try:
        document = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise DriftCheckError(f"Could not parse {label}: {exc}") from exc
    if not isinstance(document, dict):
        raise DriftCheckError(f"{label} is not an OpenAPI mapping.")
    return document


def matching_paths(spec: dict[str, Any], route: str) -> list[str]:
    """Return OpenAPI path templates with the same shape as ``route``."""
    paths = spec.get("paths")
    if not isinstance(paths, dict):
        raise DriftCheckError("OpenAPI document has no paths object.")
    key = route_key(route)
    return [path for path in paths if isinstance(path, str) and route_key(path) == key]


def download_spec(url: str, timeout: float = 60.0) -> bytes:
    """Download a public spec. The request carries no credentials."""
    if not url.startswith("https://"):
        raise DriftCheckError(f"Refusing to download a non-https spec URL: {url}")
    request = urllib.request.Request(
        url,
        headers={"User-Agent": _USER_AGENT, "Accept": "application/yaml, */*"},
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = _read_limited(response)
    except urllib.error.HTTPError as exc:
        raise DriftCheckError(f"Could not download {url}: HTTP {exc.code}.") from exc
    except urllib.error.URLError as exc:
        raise DriftCheckError(f"Could not download {url}: {exc.reason}.") from exc
    except TimeoutError as exc:
        raise DriftCheckError(f"Could not download {url}: timed out.") from exc
    if not body:
        raise DriftCheckError(f"Downloaded spec from {url} was empty.")
    return body


def _read_limited(response: Any) -> bytes:
    chunks: list[bytes] = []
    total = 0
    while total <= MAX_SPEC_BYTES:
        chunk = response.read(min(64 * 1024, MAX_SPEC_BYTES + 1 - total))
        if not chunk:
            break
        chunks.append(chunk)
        total += len(chunk)
    if total > MAX_SPEC_BYTES:
        raise DriftCheckError(f"Downloaded spec exceeds {MAX_SPEC_BYTES} bytes.")
    return b"".join(chunks)


def compare_specs(
    contract: dict[str, Any],
    snapshot: bytes,
    public: bytes,
    *,
    manifest_path: str,
    compared_label: str,
) -> DriftReport:
    """Compare public-spec bytes with the manifest checksum and the snapshot."""
    recorded = str(contract["sha256"]).lower()
    snapshot_hash = hashlib.sha256(snapshot).hexdigest()
    if snapshot_hash != recorded:
        raise DriftCheckError(
            f"{contract['snapshot']} sha256 {snapshot_hash} does not match "
            f"{manifest_path} ({recorded})."
        )
    actual = hashlib.sha256(public).hexdigest()
    routes = tuple(str(route) for route in contract["routes_used"])
    changes: tuple[RouteChange, ...] = ()
    if actual != recorded:
        changes = changed_routes(
            _decode(snapshot, str(contract["snapshot"])),
            _decode(public, compared_label),
            routes,
        )
    return DriftReport(
        name=str(contract["name"]),
        url=str(contract["url"]),
        manifest_path=manifest_path,
        recorded_sha256=recorded,
        actual_sha256=actual,
        compared_label=compared_label,
        routes=routes,
        changes=changes,
    )


def changed_routes(
    snapshot_text: str,
    public_text: str,
    routes: tuple[str, ...],
) -> tuple[RouteChange, ...]:
    """Return recorded routes whose public definition differs from the snapshot."""
    snapshot_spec = parse_spec(snapshot_text, "vendored snapshot")
    public_spec = parse_spec(public_text, "public spec")
    changes: list[RouteChange] = []
    seen: set[str] = set()
    for route in routes:
        if route in seen:
            continue
        seen.add(route)
        change = _diff_route(route, snapshot_spec, public_spec)
        if change is not None:
            changes.append(change)
    return tuple(changes)


def format_report(report: DriftReport) -> str:
    """Render the success line or the failure that names changed routes."""
    if report.matches:
        return (
            f"{report.name} matches {report.manifest_path} ({report.recorded_sha256}).\n"
            f"source: {report.url}"
        )
    lines = [
        f"{report.name} sha256 differs from {report.manifest_path}.",
        f"recorded: {report.recorded_sha256}",
        f"{report.compared_label}: {report.actual_sha256}",
        f"source: {report.url}",
        "",
    ]
    if report.changes:
        lines.append(
            "Changed paths the toolkit uses "
            f"({len(report.changes)} of {len(report.unique_routes)}):"
        )
        for change in report.changes:
            lines.append(f"  {change.route}")
            lines.extend(f"    {detail}" for detail in change.details)
        lines.append("")
        lines.append(
            f"Review the paths above, then update the vendored snapshot and {report.manifest_path}."
        )
    else:
        lines.append("None of the paths the toolkit uses changed:")
        lines.extend(f"  {route}" for route in report.unique_routes)
        lines.append("The public spec differs outside those paths.")
        lines.append("")
        lines.append(
            "Update the vendored snapshot and "
            f"{report.manifest_path} after reviewing the public spec."
        )
    return "\n".join(lines)


def annotation(report: DriftReport) -> str:
    """One GitHub Actions error line naming the changed routes."""
    if report.changes:
        names = ", ".join(change.route for change in report.changes)
    else:
        names = "none"
    summary = (
        f"{report.name} sha256 differs from {report.manifest_path}; "
        f"changed paths the toolkit uses: {names}"
    )
    return "::error::" + summary.replace("\n", " ")


class RouteChange:
    """One recorded route whose public definition changed."""

    def __init__(self, route: str, details: tuple[str, ...]) -> None:
        self.route = route
        self.details = details


class DriftReport:
    """Result of comparing the public spec bytes with the manifest."""

    def __init__(
        self,
        *,
        name: str,
        url: str,
        manifest_path: str,
        recorded_sha256: str,
        actual_sha256: str,
        compared_label: str,
        routes: tuple[str, ...],
        changes: tuple[RouteChange, ...],
    ) -> None:
        self.name = name
        self.url = url
        self.manifest_path = manifest_path
        self.recorded_sha256 = recorded_sha256
        self.actual_sha256 = actual_sha256
        self.compared_label = compared_label
        self.routes = routes
        self.changes = changes

    @property
    def matches(self) -> bool:
        return self.recorded_sha256 == self.actual_sha256

    @property
    def unique_routes(self) -> tuple[str, ...]:
        seen: set[str] = set()
        ordered: list[str] = []
        for route in self.routes:
            if route in seen:
                continue
            seen.add(route)
            ordered.append(route)
        return tuple(ordered)


def main(argv: list[str] | None = None) -> int:
    """Run the drift check and return the process exit code."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--root", type=Path, default=ROOT, help="repository root")
    parser.add_argument("--manifest", type=Path, help="path to openapi-manifest.json")
    parser.add_argument(
        "--spec-file",
        type=Path,
        help="compare this file in place of downloading the public spec",
    )
    args = parser.parse_args(argv)
    try:
        report = run_check(args.root, args.manifest, args.spec_file)
    except DriftCheckError as exc:
        print(f"OpenAPI drift check failed: {exc}", file=sys.stderr)
        return 1
    text = format_report(report)
    if report.matches:
        print(text)
        return 0
    print(text, file=sys.stderr)
    if os.environ.get("GITHUB_ACTIONS") == "true":
        print(annotation(report), file=sys.stderr)
    return 1


def run_check(root: Path, manifest_path: Path | None, spec_file: Path | None) -> DriftReport:
    """Load the manifest and compare the public spec, downloading it when needed."""
    root = root.resolve()
    manifest_file = (manifest_path or (root / "contracts" / "openapi-manifest.json")).resolve()
    manifest = load_manifest(manifest_file)
    contract = v3_contract(manifest)
    snapshot_path = root / str(contract["snapshot"])
    try:
        snapshot = snapshot_path.read_bytes()
    except OSError as exc:
        raise DriftCheckError(f"Could not read {contract['snapshot']}: {exc}") from exc
    if spec_file is None:
        public = download_spec(str(contract["url"]))
        compared_label = "public spec"
    else:
        try:
            public = spec_file.read_bytes()
        except OSError as exc:
            raise DriftCheckError(f"Could not read {spec_file}: {exc}") from exc
        compared_label = "local file"
    return compare_specs(
        contract,
        snapshot,
        public,
        manifest_path=_relative(manifest_file, root),
        compared_label=compared_label,
    )


def _relative(path: Path, root: Path) -> str:
    try:
        return path.resolve().relative_to(root).as_posix()
    except ValueError:
        return str(path)


def _decode(data: bytes, label: str) -> str:
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise DriftCheckError(f"{label} is not UTF-8: {exc}") from exc


def _diff_route(
    route: str,
    snapshot_spec: dict[str, Any],
    public_spec: dict[str, Any],
) -> RouteChange | None:
    vendored = matching_paths(snapshot_spec, route)
    public = matching_paths(public_spec, route)
    if len(vendored) > 1 or len(public) > 1:
        return RouteChange(
            route,
            (f"route shape is ambiguous: vendored {_listed(vendored)}; public {_listed(public)}",),
        )
    if not public:
        if not vendored:
            return RouteChange(route, ("not in the vendored snapshot or the public spec",))
        return RouteChange(
            route,
            (f"removed from the public spec (vendored path {vendored[0]})",),
        )
    if not vendored:
        return RouteChange(
            route,
            (f"missing from the vendored snapshot (public path {public[0]})",),
        )
    vendored_path = vendored[0]
    public_path = public[0]
    details: list[str] = []
    if vendored_path != public_path:
        details.append(f"path template {vendored_path} is {public_path} in the public spec")
    vendored_item = snapshot_spec["paths"][vendored_path]
    public_item = public_spec["paths"][public_path]
    if _canonical(vendored_item) != _canonical(public_item):
        details.append("path item changed")
    details.extend(
        _diff_refs(
            _referenced_definitions(vendored_item, snapshot_spec),
            _referenced_definitions(public_item, public_spec),
        )
    )
    if not details:
        return None
    return RouteChange(route, tuple(details))


def _listed(paths: list[str]) -> str:
    return ", ".join(paths) if paths else "none"


def _diff_refs(vendored: dict[str, Any], public: dict[str, Any]) -> list[str]:
    lines: list[str] = []
    for pointer in sorted(set(vendored) | set(public)):
        if pointer not in public:
            lines.append(f"removed referenced definition {pointer}")
        elif pointer not in vendored:
            lines.append(f"added referenced definition {pointer}")
        elif _canonical(vendored[pointer]) != _canonical(public[pointer]):
            lines.append(f"changed referenced definition {pointer}")
    if len(lines) > MAX_REF_DETAILS:
        extra = len(lines) - MAX_REF_DETAILS
        lines = lines[:MAX_REF_DETAILS]
        lines.append(f"and {extra} more referenced definitions")
    return lines


def _referenced_definitions(node: Any, spec: dict[str, Any]) -> dict[str, Any]:
    found: dict[str, Any] = {}
    seen: set[int] = set()
    stack: list[Any] = [node]
    while stack:
        current = stack.pop()
        if isinstance(current, dict | list):
            marker = id(current)
            if marker in seen:
                continue
            seen.add(marker)
        if isinstance(current, dict):
            ref = current.get("$ref")
            if isinstance(ref, str) and ref.startswith("#/") and ref not in found:
                found[ref] = _resolve_pointer(spec, ref)
                stack.append(found[ref])
            stack.extend(current.values())
        elif isinstance(current, list):
            stack.extend(current)
    return found


def _resolve_pointer(document: Any, pointer: str) -> Any:
    if not pointer.startswith("#/"):
        return {"$unresolved": pointer}
    node = document
    for raw in pointer[2:].split("/"):
        part = raw.replace("~1", "/").replace("~0", "~")
        if isinstance(node, dict) and part in node:
            node = node[part]
        elif isinstance(node, list) and part.isdigit() and int(part) < len(node):
            node = node[int(part)]
        else:
            return {"$unresolved": pointer}
    return node


def _canonical(value: Any) -> str:
    try:
        return json.dumps(_json_ready(value), sort_keys=True, separators=(",", ":"))
    except (TypeError, ValueError) as exc:
        raise DriftCheckError(f"Could not compare an OpenAPI value: {exc}") from exc


def _json_ready(value: Any, seen: set[int] | None = None) -> Any:
    if seen is None:
        seen = set()
    if isinstance(value, dict | list):
        marker = id(value)
        if marker in seen:
            raise DriftCheckError("OpenAPI document contains a circular structure.")
        seen.add(marker)
        try:
            if isinstance(value, dict):
                return {str(key): _json_ready(item, seen) for key, item in value.items()}
            return [_json_ready(item, seen) for item in value]
        finally:
            seen.remove(marker)
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    if isinstance(value, str | int | float | bool) or value is None:
        return value
    raise DriftCheckError(f"OpenAPI value is not JSON-compatible: {type(value).__name__}")


if __name__ == "__main__":
    raise SystemExit(main())
