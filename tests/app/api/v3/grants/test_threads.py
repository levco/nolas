import json
import uuid
from typing import Any
from unittest.mock import AsyncMock, Mock
from urllib.parse import urlencode

import pytest
from fastapi import FastAPI
from fastapi.routing import APIRoute
from starlette.types import Message as ASGIMessage

from app.api.middlewares.authentication import get_current_app
from app.api.payloads.messages import EmailAddress, Message
from app.api.payloads.threads import Thread
from app.api.utils.errors import create_error_response
from app.api.v3.grants import router
from app.api.v3.grants import threads as threads_api
from app.controllers.providers.base import ListThreadsParams, ListThreadsResult
from app.models.account import Account, AccountProvider
from app.models.app import App


async def request_threads(provider: Mock, grant_id: str, query: dict[str, object]) -> tuple[int, dict[str, Any]]:
    app = FastAPI()
    app.include_router(router, prefix="/v3/grants")
    app.dependency_overrides[get_current_app] = lambda: App(id=1)
    for route in app.routes:
        if isinstance(route, APIRoute) and route.endpoint is threads_api.list_threads:
            dependency = next(dep for dep in route.dependant.dependencies if dep.name == "registry")
            assert dependency.call is not None
            app.dependency_overrides[dependency.call] = lambda: Mock(get_client=lambda _: provider)
    responses: list[ASGIMessage] = []

    async def send(message: ASGIMessage) -> None:
        responses.append(message)

    await app(
        {
            "type": "http",
            "method": "GET",
            "path": f"/v3/grants/{grant_id}/threads",
            "query_string": urlencode(query).encode(),
            "headers": [],
        },
        AsyncMock(return_value={"type": "http.request", "body": b""}),
        send,
    )
    return responses[0]["status"], json.loads(b"".join(message.get("body", b"") for message in responses))


@pytest.fixture
def thread() -> Thread:
    message = Message(
        id="message-1",
        subject="Subject",
        body="Message body",
        from_=[EmailAddress(name="Sender", email="sender@example.com")],
        starred=False,
        unread=True,
        folders=["inbox"],
        grant_id="grant-1",
        date=123,
        object="message",
        snippet="Preview",
        thread_id="thread-1",
    )
    return Thread(
        id="thread-1",
        grant_id="grant-1",
        subject="Subject",
        snippet="Preview",
        latest_draft_or_message=message,
        latest_message_received_date=123,
        earliest_message_date=123,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("provider_name", list(AccountProvider))
@pytest.mark.parametrize("select", [None, " id, subject,id ", "id,latest_draft_or_message"])
async def test_thread_selection_preserves_envelope_and_nested_aliases(
    monkeypatch: pytest.MonkeyPatch,
    thread: Thread,
    provider_name: AccountProvider,
    select: str | None,
) -> None:
    account = Account(uuid=uuid.uuid4(), email="owner@example.com", provider=provider_name)
    validate = AsyncMock(return_value=(account, None))
    monkeypatch.setattr(threads_api, "validate_grant_access", validate)
    provider = Mock(list_threads=AsyncMock(return_value=ListThreadsResult([thread], "next-page")))
    query: dict[str, object] = {"limit": 10, "page_token": "previous-page", "in": "inbox"}
    if select is not None:
        query["select"] = select
    status, body = await request_threads(provider, str(account.uuid), query)
    assert status == 200
    assert set(body) == {"request_id", "data", "next_cursor"}
    assert body["request_id"] and body["next_cursor"] == "next-page"
    expected = thread.model_dump(by_alias=True, exclude_none=True)
    if select is not None:
        fields = {field.strip() for field in select.split(",")}
        expected = {key: value for key, value in expected.items() if key in fields}
    assert body["data"] == [expected]
    if select is None or "latest_draft_or_message" in select:
        message = body["data"][0]["latest_draft_or_message"]
        assert message["from"][0]["email"] == "sender@example.com"
        assert message["body"] == "Message body"
        assert "from_" not in message
    validate.assert_awaited_once_with(1, str(account.uuid))
    provider.list_threads.assert_awaited_once_with(
        account,
        ListThreadsParams(limit=10, page_token="previous-page", in_="inbox"),
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("select", ["", " ", "missing", "id,", "latest_draft_or_message.id"])
async def test_invalid_selection_never_reaches_provider(monkeypatch: pytest.MonkeyPatch, select: str) -> None:
    account = Account(uuid=uuid.uuid4(), email="owner@example.com", provider=AccountProvider.microsoft)
    monkeypatch.setattr(threads_api, "validate_grant_access", AsyncMock(return_value=(account, None)))
    provider = Mock(list_threads=AsyncMock())
    status, body = await request_threads(provider, str(account.uuid), {"select": select})
    assert status == 400
    assert body["request_id"] and body["error"]["type"] == "invalid_request_error"
    provider.list_threads.assert_not_awaited()


@pytest.mark.asyncio
async def test_selection_preserves_empty_page(monkeypatch: pytest.MonkeyPatch) -> None:
    account = Account(uuid=uuid.uuid4(), email="owner@example.com", provider=AccountProvider.microsoft)
    monkeypatch.setattr(threads_api, "validate_grant_access", AsyncMock(return_value=(account, None)))
    provider = Mock(list_threads=AsyncMock(return_value=ListThreadsResult([])))
    status, body = await request_threads(provider, str(account.uuid), {"select": "id"})
    assert status == 200 and body["data"] == []
    assert body["request_id"] and "next_cursor" not in body


@pytest.mark.asyncio
async def test_selection_preserves_grant_authorization(monkeypatch: pytest.MonkeyPatch) -> None:
    denied = create_error_response("grant.not_found", "No Grant found for this Grant ID.", 404)
    monkeypatch.setattr(threads_api, "validate_grant_access", AsyncMock(return_value=(None, denied)))
    provider = Mock(list_threads=AsyncMock())
    status, body = await request_threads(provider, str(uuid.uuid4()), {"select": "id"})
    assert status == 404 and body["error"]["type"] == "grant.not_found"
    provider.list_threads.assert_not_awaited()
