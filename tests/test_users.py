"""User discovery and entry filters against the fake API."""

import pytest

from benchling_tools.errors import AccessError, NotFoundError, UpstreamError, ValidationError
from benchling_tools.listing import get_user, list_users


async def test_user_filters_and_single_page(runtime, fake):
    fake.users[0]["memberOf"].append("org_other")
    page = await list_users(
        runtime.api,
        handles=["fixture_handle_1", "fixture_handle_2"],
        emails=["fixture1@example.test", "fixture2@example.test"],
        name="Fixture User 1",
        name_includes="Fixture",
        ids=["ent_fixture1", "ent_fixture2"],
        member_of=["org_fixture", "org_other"],
        limit=1,
        cursor="0",
    )
    assert fake.calls[-1].params == {
        "handles": "fixture_handle_1,fixture_handle_2",
        "email.anyOf": "fixture1@example.test,fixture2@example.test",
        "name": "Fixture User 1",
        "nameIncludes": "Fixture",
        "ids": "ent_fixture1,ent_fixture2",
        "memberOf": "org_fixture,org_other",
        "pageSize": "1",
        "nextToken": "0",
    }
    assert page["items"] == [
        {
            "id": "ent_fixture1",
            "handle": "fixture_handle_1",
            "name": "Fixture User 1",
            "email": "fixture1@example.test",
            "is_suspended": False,
        }
    ]
    assert page["routing"]["client_side"] == []
    assert page["next_token"] is None
    page = await list_users(runtime.api, limit=1)
    assert page["next_token"] == "1"
    assert (await list_users(runtime.api, limit=1, cursor=page["next_token"]))["items"][0][
        "id"
    ] == "ent_fixture2"
    assert fake.writes() == []


async def test_get_user_by_id_and_handle(runtime, fake):
    fake.users[0]["passwordLastChangedAt"] = "private"
    fake.users[0]["apiKeyLastChangedAt"] = "private"
    by_id = await get_user(runtime.api, "ent_fixture1")
    by_handle = await get_user(runtime.api, "fixture_handle_1")
    assert by_id == by_handle
    assert set(by_id) == {"id", "handle", "name", "email", "is_suspended"}
    assert [c.path for c in fake.calls] == ["/api/v2/users/ent_fixture1", "/api/v2/users"]


async def test_handle_errors_and_permission_hint(runtime, fake):
    with pytest.raises(NotFoundError) as error:
        await get_user(runtime.api, "missing_fixture")
    assert error.value.details["handle"] == "missing_fixture"
    fake.users.append({**fake.users[0], "id": "ent_fixture3"})
    with pytest.raises(ValidationError) as error:
        await get_user(runtime.api, "fixture_handle_1")
    assert len(error.value.details["matches"]) == 2
    fake.payload_overrides.append(("/users", 403, {"error": "forbidden"}))
    with pytest.raises(AccessError) as error:
        await list_users(runtime.api)
    assert error.value.details["status"] == 403
    assert "organization" in error.value.details["hint"]


@pytest.mark.parametrize(
    "kwargs",
    [
        {"emails": ["fixture@example.test"] * 101},
        {"handles": ["a,b"]},
        {"ids": "ent_fixture1"},
        {"member_of": [""]},
        {"limit": 0},
        {"cursor": ""},
        {"name": " "},
        {"name_includes": 1},
    ],
)
async def test_bad_user_filters_do_not_call_api(runtime, fake, kwargs):
    with pytest.raises(ValidationError):
        await list_users(runtime.api, **kwargs)
    assert fake.calls == []


async def test_invalid_user_lookup_responses(runtime, fake):
    with pytest.raises(ValidationError):
        await get_user(runtime.api, "ent_../bad")
    fake.payload_overrides.append(("/users", 200, {"users": [], "nextToken": "more"}))
    with pytest.raises(ValidationError, match="more than one page"):
        await get_user(runtime.api, "fixture_handle_1")
    fake.payload_overrides.append(
        ("/users", 200, {"users": [{"id": "bad", "handle": "fixture_handle_1"}]})
    )
    with pytest.raises(UpstreamError, match="invalid user ID"):
        await get_user(runtime.api, "fixture_handle_1")


@pytest.mark.parametrize(
    ("kwargs", "path", "creator_key", "author_key"),
    [
        ({}, "/api/v3/entry/items", "creator.anyOf", "authors.anyOf"),
        ({"folder": "lib_fixture"}, "/api/v2/entries", "creatorIds", "authorIds.anyOf"),
        ({"name": "fixture, entry"}, "/api/v2/entries", "creatorIds", "authorIds.anyOf"),
    ],
)
async def test_entry_user_filters_resolve_once_and_route(
    runtime, fake, kwargs, path, creator_key, author_key
):
    fake.extra_entries.append(
        {
            "id": "etr_fixture",
            "name": "fixture, entry",
            "displayId": "EXP-FIXTURE",
            "modifiedAt": "2026-01-01T00:00:00Z",
            "schemaId": "entsch_fixture",
            "projectId": "src_fixture",
            "folderId": "lib_fixture",
            "creatorId": "ent_fixture1",
            "authorIds": ["ent_fixture2"],
        }
    )
    page = await runtime.service.list_entries(
        creator=["fixture_handle_1", "ent_fixture2"], author=["fixture_handle_2"], **kwargs
    )
    assert [item["id"] for item in page["items"]] == ["etr_fixture"]
    assert page["resolved_users"] == {
        "creator": ["ent_fixture1", "ent_fixture2"],
        "author": ["ent_fixture2"],
    }
    assert page["routing"]["server_side"][-2:] == ["creator", "author"]
    assert [c.path for c in fake.calls] == ["/api/v2/users", path]
    assert fake.calls[0].params["handles"] == "fixture_handle_1,fixture_handle_2"
    assert fake.calls[1].params[creator_key] == "ent_fixture1,ent_fixture2"
    assert fake.calls[1].params[author_key] == "ent_fixture2"


async def test_entry_user_validation_before_calls(runtime, fake):
    with pytest.raises(ValidationError):
        await runtime.service.list_entries(folder="lib_fixture", author=["ent_fixture"] * 101)
    with pytest.raises(ValidationError):
        await runtime.service.list_entries(author=["ent_fixture"] * 101)
    with pytest.raises(ValidationError):
        await runtime.service.list_entries(creator=["ent_fixture"] * 101)
    with pytest.raises(NotFoundError):
        await runtime.service.list_entries(creator=["missing_fixture"])
    assert not any(c.path.endswith("/entries") for c in fake.calls)
