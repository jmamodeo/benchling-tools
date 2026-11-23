"""Paged, read-only discovery of entries, schemas, and dropdown options.

Entry filters are sent to the API version that actually applies them. V3
``GET /entry/items`` has no folder parameter and live tenants ignore
``folderId`` on that route, so a folder filter uses V2 ``GET /entries``.
A name that contains a comma also uses V2. ``name.anyOf`` and
``names.anyOf`` are comma-separated lists, and the contract documents no
escape for a comma inside one name. V2 ``name`` is a single entry name.
"""

from __future__ import annotations

import base64
import json
import re
from datetime import UTC, datetime
from typing import Any

from .api import BenchlingAPI
from .errors import AccessError, NotFoundError, UpstreamError, ValidationError

LIST_LIMIT = 50
LIST_LIMIT_MAX = 100
SCHEMA_KINDS = ("entry", "entity", "result", "run")
SCHEMA_PATHS = {
    "entry": "/entry-schema/items",
    "entity": "/entity-schema/items",
    "result": "/result-schema/items",
    "run": "/run-schema/items",
}

# Filters each entry-list route applies on the server. Nothing is filtered locally.
V3_ENTRY_FILTERS = ("project", "schema", "name", "modified_at", "creator", "author")
V2_ENTRY_FILTERS = ("folder", "project", "schema", "name", "modified_at", "creator", "author")


def schema_collection(schema_id: str) -> str:
    """Return the V3 collection path segment for a schema id prefix."""
    if schema_id.startswith("es_"):
        return "entity-schema"
    if schema_id.startswith("assaysch_"):
        return "result-schema"
    if schema_id.startswith("runsch_"):
        return "run-schema"
    return "entry-schema"


def check_limit(limit: object) -> int:
    """Require a page size in the Benchling range of 1 to 100. Booleans are rejected."""
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= LIST_LIMIT_MAX:
        raise ValidationError("limit must be between 1 and 100")
    return limit


def _text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValidationError(f"{label} must be a non-empty string")
    text = value.strip()
    if "," in text:
        raise ValidationError(f"{label} must not contain a comma")
    return text


def _optional_text(value: object, label: str) -> str | None:
    if value is None:
        return None
    return _text(value, label)


def _entry_name(value: object) -> str | None:
    """One entry name. A comma is allowed; the caller chooses a parameter that can carry it."""
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise ValidationError("name must be a non-empty string")
    return value.strip()


def _bound(value: object, *, end_of_day: bool) -> datetime | None:
    """Parse a date or an RFC3339 timestamp that includes a time zone.

    A bare date is the start or end of that UTC day. A timestamp with no zone
    is rejected.
    """
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise ValidationError("Modified date must be YYYY-MM-DD or an RFC3339 timestamp")
    text = value.strip()
    if len(text) == 10 and text[4] == "-" and text[7] == "-":
        text = f"{text}T23:59:59Z" if end_of_day else f"{text}T00:00:00Z"
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValidationError("Modified date must be YYYY-MM-DD or an RFC3339 timestamp") from exc
    if parsed.tzinfo is None or parsed.tzinfo.utcoffset(parsed) is None:
        raise ValidationError(
            "Modified timestamp has no time zone. Use YYYY-MM-DD or an RFC3339 "
            "timestamp with a zone, such as 2026-06-15T12:00:00Z."
        )
    return parsed.astimezone(UTC)


def _timestamp_fraction(value: str) -> str:
    """The source fractional seconds, including the dot, so ``.500`` stays ``.500``."""
    start = value.find(".")
    if start == -1:
        return ""
    end = start + 1
    while end < len(value) and value[end].isdigit():
        end += 1
    if end == start + 1:
        return ""
    return value[start:end]


def utc_timestamp(value: object) -> object:
    """Rewrite a timezone-aware timestamp as UTC with a ``Z`` suffix.

    V2 listing responses use ``+00:00`` and V3 responses use ``Z``. A value that
    is not a timezone-aware timestamp is returned unchanged.
    """
    if not isinstance(value, str) or not value:
        return value
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return value
    if parsed.tzinfo is None or parsed.tzinfo.utcoffset(parsed) is None:
        return value
    utc = parsed.astimezone(UTC)
    return f"{utc.strftime('%Y-%m-%dT%H:%M:%S')}{_timestamp_fraction(value)}Z"


def _stamp(value: datetime) -> str:
    return value.strftime("%Y-%m-%dT%H:%M:%SZ")


def _modified_range(after: object, before: object) -> tuple[datetime | None, datetime | None]:
    start = _bound(after, end_of_day=False)
    end = _bound(before, end_of_day=True)
    if start is not None and end is not None and start > end:
        raise ValidationError("modified_after must not be later than modified_before")
    return start, end


def _v2_modified(start: datetime | None, end: datetime | None) -> str | None:
    """Inclusive V2 ``modifiedAt`` expression. V2 takes one comparison string."""
    parts: list[str] = []
    if start is not None:
        parts.append(f">= {_stamp(start)}")
    if end is not None:
        parts.append(f"<= {_stamp(end)}")
    return " AND ".join(parts) or None


def _items(payload: dict[str, Any], *, path: str, key: str = "items") -> list[dict[str, Any]]:
    raw = payload.get(key)
    if not isinstance(raw, list):
        raise UpstreamError(
            "Benchling pagination response has an unexpected shape",
            details={"path": path, "expected_key": key, "keys": sorted(payload)},
        )
    items: list[dict[str, Any]] = []
    for item in raw:
        if not isinstance(item, dict):
            raise UpstreamError("Benchling returned a non-object list item", details={"path": path})
        items.append(item)
    return items


def _next_token(payload: dict[str, Any]) -> str | None:
    token = payload.get("nextToken")
    if token is None or token == "":
        return None
    if not isinstance(token, str):
        raise UpstreamError("Benchling returned a pagination token that is not a string")
    return token


def _values(value: object, label: str, *, maximum: int | None = None) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, (list, tuple)):
        raise ValidationError(f"{label} must be a list")
    if maximum is not None and len(value) > maximum:
        raise ValidationError(f"{label} accepts at most {maximum} values")
    return [_text(item, label) for item in value]


def _user_summary(item: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": item.get("id"),
        "handle": item.get("handle"),
        "name": item.get("name"),
        "email": item.get("email"),
        "is_suspended": item.get("isSuspended"),
    }


async def _users_request(api: BenchlingAPI, path: str, params: dict[str, Any]) -> dict[str, Any]:
    try:
        return await api.request("GET", path, version=2, params=params)
    except AccessError as exc:
        exc.details["hint"] = (
            "Listing users requires the caller to share an organization with them "
            "or have tenant administrator access."
        )
        raise


async def list_users(
    api: BenchlingAPI,
    *,
    handles: object = None,
    emails: object = None,
    name: object = None,
    name_includes: object = None,
    ids: object = None,
    member_of: object = None,
    limit: object = LIST_LIMIT,
    cursor: object = None,
) -> dict[str, Any]:
    """List a single V2 page of visible users."""
    page_size = check_limit(limit)
    params: dict[str, Any] = {"pageSize": page_size}
    for key, value, label, maximum in (
        ("handles", handles, "handle", None),
        ("email.anyOf", emails, "email", 100),
        ("ids", ids, "id", None),
        ("memberOf", member_of, "member_of", None),
    ):
        values = _values(value, label, maximum=maximum)
        if values:
            params[key] = ",".join(values)
    for key, value in (("name", name), ("nameIncludes", name_includes)):
        if value is not None:
            if not isinstance(value, str) or not value.strip():
                raise ValidationError(f"{key} must be a non-empty string")
            params[key] = value.strip()
    if cursor is not None:
        if not isinstance(cursor, str) or not cursor:
            raise ValidationError("cursor must be a non-empty string")
        params["nextToken"] = cursor
    payload = await _users_request(api, "/users", params)
    return {
        "items": [_user_summary(item) for item in _items(payload, path="/users", key="users")],
        "next_token": _next_token(payload),
        "routing": {
            "api_version": 2,
            "path": "/users",
            "server_side": ["handle", "email", "name", "name_includes", "id", "member_of"],
            "client_side": [],
        },
    }


async def _resolve_handles(api: BenchlingAPI, handles: list[str]) -> dict[str, dict[str, Any]]:
    if not handles:
        return {}
    page = await list_users(api, handles=list(dict.fromkeys(handles)), limit=100)
    if page["next_token"]:
        raise ValidationError("User lookup returned more than one page; narrow the handle lookup")
    resolved: dict[str, dict[str, Any]] = {}
    for handle in dict.fromkeys(handles):
        matches = [item for item in page["items"] if item["handle"] == handle]
        if not matches:
            raise NotFoundError("User handle was not found", details={"handle": handle})
        if len(matches) != 1:
            raise ValidationError(
                "User handle is ambiguous", details={"handle": handle, "matches": matches}
            )
        user_id = matches[0]["id"]
        if not isinstance(user_id, str) or not re.fullmatch(r"ent_[A-Za-z0-9_]+", user_id):
            raise UpstreamError("Benchling returned an invalid user ID")
        resolved[handle] = matches[0]
    return resolved


async def get_user(api: BenchlingAPI, reference: object) -> dict[str, Any]:
    """Get one user by ID or exact handle."""
    ref = _text(reference, "user")
    if ref.startswith("ent_"):
        if not re.fullmatch(r"ent_[A-Za-z0-9_]+", ref):
            raise ValidationError("Invalid user ID")
        return _user_summary(await _users_request(api, f"/users/{ref}", {}))
    return (await _resolve_handles(api, [ref]))[ref]


def entry_web_url(item: object) -> str | None:
    """The entry's Benchling web-app link: V3 ``webUrl`` or V2 ``webURL``, else None."""
    if not isinstance(item, dict):
        return None
    for key in ("webUrl", "webURL"):
        value = item.get(key)
        if isinstance(value, str) and value:
            return value
    return None


def entry_summary(item: dict[str, Any]) -> dict[str, Any]:
    """One entry row, the same shape from a V3 item or a V2 entry."""
    schema = item.get("typedSchema") or item.get("schema")
    if isinstance(schema, dict):
        schema_id = schema.get("id")
    elif isinstance(schema, str):
        schema_id = schema
    else:
        schema_id = item.get("schemaId")
    folder = item.get("folder")
    if isinstance(folder, dict):
        folder_id = folder.get("id")
    else:
        folder_id = item.get("folderId")
    project = item.get("project") or item.get("reviewSource")
    if isinstance(project, dict):
        project_id = project.get("id")
    else:
        project_id = item.get("projectId")
    return {
        "id": item.get("id"),
        "name": item.get("name"),
        "display_id": item.get("displayId"),
        "modified_at": utc_timestamp(item.get("modifiedAt")),
        "schema_id": schema_id,
        "project_id": project_id,
        "folder_id": folder_id,
        "web_url": entry_web_url(item),
    }


async def _one_page(
    api: BenchlingAPI,
    path: str,
    params: dict[str, Any],
    *,
    version: int,
    item_key: str,
    limit: int,
) -> tuple[list[dict[str, Any]], str | None]:
    """Read upstream pages until ``limit`` items are collected or the list ends."""
    query = dict(params)
    query["pageSize"] = limit
    collected: list[dict[str, Any]] = []
    seen: set[str] = set()
    token: str | None = query.get("nextToken")
    if isinstance(token, str):
        seen.add(token)
    while len(collected) < limit:
        query["pageSize"] = limit - len(collected)
        payload = await api.request("GET", path, version=version, params=query)
        page = _items(payload, path=path, key=item_key)
        if len(page) > query["pageSize"]:
            raise UpstreamError(
                "Benchling returned more items than pageSize",
                details={"path": path, "page_size": query["pageSize"], "returned": len(page)},
            )
        collected.extend(page)
        token = _next_token(payload)
        if not token:
            return collected, None
        if token in seen:
            raise UpstreamError(
                "Benchling returned a repeated pagination token",
                details={"path": path},
            )
        seen.add(token)
        query["nextToken"] = token
    return collected, token


async def list_entries(
    api: BenchlingAPI,
    *,
    project: object = None,
    folder: object = None,
    schema_id: object = None,
    name: object = None,
    modified_after: object = None,
    modified_before: object = None,
    creator: object = None,
    author: object = None,
    limit: object = LIST_LIMIT,
    cursor: object = None,
) -> dict[str, Any]:
    """Return one page of entries.

    Without a folder, and when the name has no comma, filters are V3 query
    parameters. A folder filter, or a name that contains a comma, sends the
    whole query to V2 so every filter is applied together.
    """
    page_size = check_limit(limit)
    project_id = _optional_text(project, "project")
    folder_id = _optional_text(folder, "folder")
    schema = _optional_text(schema_id, "schema")
    entry_name = _entry_name(name)
    start, end = _modified_range(modified_after, modified_before)
    if cursor is not None and (not isinstance(cursor, str) or not cursor):
        raise ValidationError("cursor must be a non-empty string")
    # V3 name.anyOf and V2 names.anyOf split on commas. V2 name is one name.
    comma_name = entry_name is not None and "," in entry_name
    creators = _values(creator, "creator", maximum=100)
    authors = _values(author, "author", maximum=100)
    for ref in creators + authors:
        if ref.startswith("ent_") and not re.fullmatch(r"ent_[A-Za-z0-9_]+", ref):
            raise ValidationError("Invalid user ID")
    handles = [ref for ref in creators + authors if not ref.startswith("ent_")]
    resolved = await _resolve_handles(api, handles)
    creator_ids = [resolved[ref]["id"] if ref in resolved else ref for ref in creators]
    author_ids = [resolved[ref]["id"] if ref in resolved else ref for ref in authors]

    if folder_id is not None or comma_name:
        params: dict[str, Any] = {}
        if creator_ids:
            params["creatorIds"] = ",".join(creator_ids)
        if author_ids:
            params["authorIds.anyOf"] = ",".join(author_ids)
        if folder_id is not None:
            params["folderId"] = folder_id
        if project_id is not None:
            params["projectId"] = project_id
        if schema is not None:
            params["schemaId"] = schema
        if entry_name is not None:
            if comma_name:
                params["name"] = entry_name
            else:
                params["names.anyOf"] = entry_name
        modified = _v2_modified(start, end)
        if modified is not None:
            params["modifiedAt"] = modified
        if isinstance(cursor, str):
            params["nextToken"] = cursor
        items, token = await _one_page(
            api, "/entries", params, version=2, item_key="entries", limit=page_size
        )
        routing = {
            "api_version": 2,
            "path": "/entries",
            "server_side": list(V2_ENTRY_FILTERS),
            "client_side": [],
        }
    else:
        params = {}
        if creator_ids:
            params["creator.anyOf"] = ",".join(creator_ids)
        if author_ids:
            params["authors.anyOf"] = ",".join(author_ids)
        if project_id is not None:
            params["project.eq"] = project_id
        if schema is not None:
            params["schema.eq"] = schema
        if entry_name is not None:
            params["name.anyOf"] = entry_name
        if start is not None:
            params["modifiedAt.gte"] = _stamp(start)
        if end is not None:
            params["modifiedAt.lte"] = _stamp(end)
        if isinstance(cursor, str):
            params["nextToken"] = cursor
        items, token = await _one_page(
            api, "/entry/items", params, version=3, item_key="items", limit=page_size
        )
        routing = {
            "api_version": 3,
            "path": "/entry/items",
            "server_side": list(V3_ENTRY_FILTERS),
            "client_side": [],
        }
    result = {
        "items": [entry_summary(item) for item in items],
        "next_token": token,
        "routing": routing,
    }
    if creators or authors:
        result["resolved_users"] = {"creator": creator_ids, "author": author_ids}
    return result


def _encode_cursor(kind: str, token: str | None) -> str:
    raw = json.dumps({"kind": kind, "token": token}, separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _decode_cursor(cursor: object, order: tuple[str, ...] | list[str]) -> tuple[int, str | None]:
    if not isinstance(cursor, str) or not cursor:
        raise ValidationError("cursor must be a non-empty string")
    stripped = cursor.rstrip("=")
    padded = stripped + "=" * (-len(stripped) % 4)
    try:
        payload = json.loads(base64.urlsafe_b64decode(padded))
    except (ValueError, json.JSONDecodeError) as exc:
        raise ValidationError("Schema page token is not valid") from exc
    if not isinstance(payload, dict):
        raise ValidationError("Schema page token is not valid")
    kind = payload.get("kind")
    token = payload.get("token")
    if (
        not isinstance(kind, str)
        or kind not in order
        or (token is not None and not isinstance(token, str))
    ):
        raise ValidationError(
            "Schema page token does not match this list",
            details={"kind": kind},
        )
    if token == "":
        token = None
    return order.index(kind), token


def _schema_summary(item: dict[str, Any], kind: str) -> dict[str, Any]:
    return {
        "id": item.get("id"),
        "name": item.get("name"),
        "kind": kind,
        "modified_at": utc_timestamp(item.get("modifiedAt")),
    }


async def list_schemas(
    api: BenchlingAPI,
    *,
    kind: object = None,
    name: object = None,
    limit: object = LIST_LIMIT,
    cursor: object = None,
) -> dict[str, Any]:
    """Return one page of entry, result, and run schemas.

    Upstream pages are followed until the page is full or every requested kind
    is exhausted. The next token resumes at the following upstream page.
    """
    page_size = check_limit(limit)
    schema_name = _optional_text(name, "name")
    if kind is None:
        order = list(SCHEMA_KINDS)
    elif kind in SCHEMA_KINDS:
        order = [str(kind)]
    else:
        raise ValidationError(
            "Schema kind must be entry, result, or run",
            details={"supported": list(SCHEMA_KINDS)},
        )
    index = 0
    token: str | None = None
    if cursor is not None:
        index, token = _decode_cursor(cursor, order)

    items: list[dict[str, Any]] = []
    seen: dict[str, set[str]] = {}
    while index < len(order) and len(items) < page_size:
        current = order[index]
        path = SCHEMA_PATHS[current]
        while len(items) < page_size:
            params: dict[str, Any] = {"pageSize": page_size - len(items)}
            if schema_name is not None:
                params["name.anyOf.caseSensitive"] = schema_name
            if token:
                params["nextToken"] = token
            payload = await api.request("GET", path, params=params)
            page = _items(payload, path=path)
            if len(page) > params["pageSize"]:
                raise UpstreamError(
                    "Benchling returned more items than pageSize",
                    details={
                        "path": path,
                        "page_size": params["pageSize"],
                        "returned": len(page),
                    },
                )
            items.extend(_schema_summary(item, current) for item in page)
            token = _next_token(payload)
            if not token:
                break
            seen_tokens = seen.setdefault(current, set())
            if token in seen_tokens:
                raise UpstreamError(
                    "Benchling returned a repeated pagination token",
                    details={"path": path},
                )
            seen_tokens.add(token)
        if len(items) >= page_size:
            break
        index += 1
        token = None

    next_token: str | None = None
    if len(items) >= page_size:
        if token:
            next_token = _encode_cursor(order[index], token)
        elif index + 1 < len(order):
            next_token = _encode_cursor(order[index + 1], None)
    return {"items": items, "next_token": next_token, "kinds": order}


def dropdown_id_of(field: dict[str, Any]) -> str | None:
    """Return the dropdown id a field definition points at, if it has one."""
    direct = field.get("dropdownId")
    if isinstance(direct, str) and direct:
        return direct
    link = field.get("linkDefinition")
    if isinstance(link, dict):
        linked = link.get("id")
        if isinstance(linked, str) and linked:
            return linked
    return None


async def load_dropdown(api: BenchlingAPI, dropdown_id: str) -> dict[str, Any]:
    """Fetch one dropdown and every option on ``/dropdown/{id}/options/items``."""
    dropdown = await api.request("GET", f"/dropdown/{dropdown_id}")
    options = await api.request("GET", f"/dropdown/{dropdown_id}/options/items")
    raw = options.get("items")
    if not isinstance(raw, list):
        raise UpstreamError(
            "Dropdown options response has an unexpected shape",
            details={"dropdown_id": dropdown_id, "keys": sorted(options)},
        )
    parsed: list[dict[str, Any]] = []
    for item in raw:
        if not isinstance(item, dict):
            raise UpstreamError(
                "Benchling returned a non-object dropdown option",
                details={"dropdown_id": dropdown_id},
            )
        parsed.append(
            {
                "id": item.get("id"),
                "name": item.get("name"),
                "archived": bool(item.get("archived")),
            }
        )
    return {
        "id": dropdown.get("id", dropdown_id),
        "name": dropdown.get("name"),
        "options": parsed,
    }
