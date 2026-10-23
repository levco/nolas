import json
import uuid
from typing import Any
from unittest.mock import AsyncMock, Mock
from urllib.parse import urlencode

import pytest
from fastapi import FastAPI
from fastapi.routing import APIRoute
from starlette.types import Message

from app.api.middlewares.authentication import get_current_app
from app.api.payloads.folders import Folder
from app.api.utils.errors import create_error_response
from app.api.v3.grants import folders as folders_api
from app.api.v3.grants import router
from app.controllers.providers.base import ListFoldersParams
from app.controllers.providers.exceptions import ProviderError
from app.models.account import Account, AccountProvider
from app.models.app import App


@pytest.fixture
def account() -> Account:
    return Account(uuid=uuid.uuid4(), email="owner@example.com", provider=AccountProvider.microsoft)


async def request_folders(provider: Mock, grant_id: str, query: dict[str, object]) -> tuple[int, dict[str, Any]]:
    app = FastAPI()
    app.include_router(router, prefix="/v3/grants")
    app.dependency_overrides[get_current_app] = lambda: App(id=1)
    for route in app.routes:
        if isinstance(route, APIRoute) and route.endpoint is folders_api.list_folders:
            dependency = next(dep for dep in route.dependant.dependencies if dep.name == "registry")
            assert dependency.call is not None
            app.dependency_overrides[dependency.call] = lambda: Mock(get_client=lambda _: provider)
    responses: list[Message] = []

    async def send(message: Message) -> None:
        responses.append(message)

    await app(
        {
            "type": "http",
            "method": "GET",
            "path": f"/v3/grants/{grant_id}/folders",
            "query_string": urlencode(query).encode(),
            "headers": [],
        },
        AsyncMock(return_value={"type": "http.request", "body": b""}),
        send,
    )
    return responses[0]["status"], json.loads(b"".join(message.get("body", b"") for message in responses))


@pytest.mark.asyncio
async def test_list_route_paginates_and_selects_fields(monkeypatch: pytest.MonkeyPatch, account: Account) -> None:
    validate = AsyncMock(return_value=(account, None))
    monkeypatch.setattr(folders_api, "validate_grant_access", validate)
    provider = Mock(
        list_folders=AsyncMock(
            return_value=[
                Folder(id=str(index), name=f"Folder {index}", grant_id=str(account.uuid)) for index in range(51)
            ]
        )
    )

    status, first = await request_folders(provider, str(account.uuid), {})
    assert status == 200
    assert len(first["data"]) == 50
    assert first["next_cursor"] == "50"
    assert first["request_id"]
    assert first["data"][0]["object"] == "folder"
    provider.list_folders.assert_awaited_once_with(account, ListFoldersParams())
    validate.assert_awaited_once_with(1, str(account.uuid))

    status, last = await request_folders(
        provider,
        str(account.uuid),
        {
            "page_token": first["next_cursor"],
            "select": "id,name",
        },
    )
    assert status == 200
    assert last["data"] == [{"id": "50", "name": "Folder 50"}]
    assert last.get("next_cursor") is None


@pytest.mark.asyncio
async def test_microsoft_filters_reach_provider(monkeypatch: pytest.MonkeyPatch, account: Account) -> None:
    monkeypatch.setattr(folders_api, "validate_grant_access", AsyncMock(return_value=(account, None)))
    provider = Mock(list_folders=AsyncMock(return_value=[]))
    status, body = await request_folders(
        provider,
        str(account.uuid),
        {
            "include_hidden_folders": "true",
            "single_level": "true",
            "parent_id": "a/b",
            "shared_from": "shared@example.com",
            "limit": 200,
        },
    )
    assert status == 200 and body["data"] == []
    provider.list_folders.assert_awaited_once_with(
        account,
        ListFoldersParams(
            include_hidden_folders=True,
            single_level=True,
            parent_id="a/b",
            shared_from="shared@example.com",
        ),
    )


@pytest.mark.asyncio
async def test_shared_grant_is_resolved_within_current_app(monkeypatch: pytest.MonkeyPatch, account: Account) -> None:
    shared = Account(uuid=uuid.uuid4(), email="shared@example.com")
    validate = AsyncMock(side_effect=[(account, None), (shared, None)])
    monkeypatch.setattr(folders_api, "validate_grant_access", validate)
    provider = Mock(list_folders=AsyncMock(return_value=[]))
    status, _ = await request_folders(provider, str(account.uuid), {"shared_from": str(shared.uuid)})
    assert status == 200
    assert validate.await_args_list[-1].args == (1, str(shared.uuid))
    assert provider.list_folders.await_args.args[1].shared_from == shared.email


@pytest.mark.asyncio
@pytest.mark.parametrize("shared", [False, True])
async def test_inaccessible_grant_never_reaches_provider(
    monkeypatch: pytest.MonkeyPatch, account: Account, shared: bool
) -> None:
    denied = create_error_response("grant.not_found", "No Grant found for this Grant ID.", 404)
    validate = AsyncMock(side_effect=[(account, None), (None, denied)] if shared else [(None, denied)])
    monkeypatch.setattr(folders_api, "validate_grant_access", validate)
    provider = Mock(list_folders=AsyncMock())
    status, body = await request_folders(
        provider, str(account.uuid), {"shared_from": str(uuid.uuid4())} if shared else {}
    )
    assert status == 404 and body["error"]["type"] == "grant.not_found"
    provider.list_folders.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "query",
    [
        {"page_token": "invalid"},
        {"page_token": "-1"},
        {"page_token": ""},
        {"page_token": "²"},
        {"page_token": "9" * 5000},
        {"select": "missing"},
        {"select": ""},
        {"shared_from": "invalid-grant"},
        {"shared_from": "bad@@example.com"},
        {"shared_from": "user@"},
        {"shared_from": "@example.com"},
        {"shared_from": "one@example.com,two@example.com"},
        {"shared_from": "Name <one@example.com>"},
        {"shared_from": "a b@example.com"},
        {"shared_from": "one@example.com\r\nBcc:two@example.com"},
        {"shared_from": ""},
    ],
)
async def test_invalid_cursor_and_fields_return_nylas_errors(
    monkeypatch: pytest.MonkeyPatch, account: Account, query: dict[str, Any]
) -> None:
    monkeypatch.setattr(folders_api, "validate_grant_access", AsyncMock(return_value=(account, None)))
    provider = Mock(list_folders=AsyncMock())
    status, body = await request_folders(provider, str(account.uuid), query)
    assert status == 400
    assert body["request_id"] and body["error"]["type"] == "invalid_request_error"
    provider.list_folders.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("query", [{"limit": 0}, {"limit": 201}, {"limit": "bad"}, {"single_level": "bad"}])
async def test_invalid_query_is_rejected_before_provider(
    monkeypatch: pytest.MonkeyPatch, account: Account, query: dict[str, Any]
) -> None:
    validate = AsyncMock(return_value=(account, None))
    monkeypatch.setattr(folders_api, "validate_grant_access", validate)
    provider = Mock(list_folders=AsyncMock())
    status, body = await request_folders(provider, str(account.uuid), query)
    assert status == 400
    assert body["request_id"] and body["error"]["type"] == "invalid_request_error"
    validate.assert_not_awaited()
    provider.list_folders.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "error,expected",
    [
        (ProviderError("Provider failure", status_code=403), 403),
        (ProviderError("Provider failure", status_code=429), 429),
        (RuntimeError("private details"), 500),
    ],
)
async def test_errors_preserve_status_without_exposing_internal_details(
    monkeypatch: pytest.MonkeyPatch, account: Account, error: Exception, expected: int
) -> None:
    monkeypatch.setattr(folders_api, "validate_grant_access", AsyncMock(return_value=(account, None)))
    provider = Mock(list_folders=AsyncMock(side_effect=error))
    status, body = await request_folders(provider, str(account.uuid), {})
    assert status == expected
    assert body["request_id"] and "error" in body
    assert "private details" not in json.dumps(body)


def test_folder_listing_is_documented() -> None:
    app = FastAPI()
    app.include_router(router, prefix="/v3/grants")
    operation = app.openapi()["paths"]["/v3/grants/{grant_id}/folders"]["get"]
    assert {param["name"] for param in operation["parameters"]} == {
        "grant_id",
        "limit",
        "page_token",
        "parent_id",
        "shared_from",
        "single_level",
        "select",
        "include_hidden_folders",
    }
    assert operation["responses"]["200"]["content"]["application/json"]["schema"]["$ref"].endswith(
        "/FolderListResponse"
    )
