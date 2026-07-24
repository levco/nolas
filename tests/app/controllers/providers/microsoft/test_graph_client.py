import asyncio
import sys
import types
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock

from app.controllers.providers.base import ListThreadsParams

# The concrete HTTP client pulls in application repository dependencies that are
# unrelated to this provider unit test.
http_module = types.ModuleType("app.controllers.providers.http")
http_module.AuthorizedHttpClient = object
sys.modules.setdefault("app.controllers.providers.http", http_module)

from app.controllers.providers.microsoft.graph_client import GraphClient  # noqa: E402


def test_list_threads_requests_newest_messages_first() -> None:
    http = SimpleNamespace(request=AsyncMock(return_value={"value": []}))
    client = GraphClient(http)
    account = SimpleNamespace(uuid=uuid.uuid4(), email="owner@example.com")

    asyncio.run(client.list_threads(account, ListThreadsParams(limit=20)))

    query = http.request.await_args.kwargs["params"]
    assert query["$orderby"] == "receivedDateTime desc"
    assert query["$filter"].startswith("receivedDateTime ne null and ")


def test_list_threads_filters_on_latest_message_unread_state() -> None:
    thread_id = "conversation-1"
    latest = {
        "id": "latest",
        "conversationId": thread_id,
        "subject": "Re: Hello",
        "bodyPreview": "Reply",
        "receivedDateTime": "2026-07-23T12:00:00Z",
        "isRead": True,
        "isDraft": False,
    }
    older = {
        "id": "older",
        "conversationId": thread_id,
        "subject": "Hello",
        "bodyPreview": "Original",
        "receivedDateTime": "2026-07-22T12:00:00Z",
        "isRead": False,
        "isDraft": False,
    }
    http = SimpleNamespace(request=AsyncMock(return_value={"value": [latest, older]}))
    client = GraphClient(http)
    account = SimpleNamespace(uuid=uuid.uuid4(), email="owner@example.com")

    result = asyncio.run(client.list_threads(account, ListThreadsParams(limit=20, unread=False)))

    assert [thread.id for thread in result.threads] == [thread_id]
    assert result.threads[0].unread is False
    assert "isRead eq true" in http.request.await_args.kwargs["params"]["$filter"]
    assert http.request.await_count == 1


def test_list_threads_pushes_unread_filter_to_graph() -> None:
    http = SimpleNamespace(request=AsyncMock(return_value={"value": []}))
    client = GraphClient(http)
    account = SimpleNamespace(uuid=uuid.uuid4(), email="owner@example.com")

    asyncio.run(client.list_threads(account, ListThreadsParams(limit=20, unread=True)))

    assert "isRead eq false" in http.request.await_args.kwargs["params"]["$filter"]


def _raw_graph_message(message_id: str, conversation_id: str, is_read: bool) -> dict[str, object]:
    return {
        "id": message_id,
        "conversationId": conversation_id,
        "subject": "Hello",
        "bodyPreview": "Preview",
        "body": {"content": "Body"},
        "receivedDateTime": "2026-07-23T12:00:00Z",
        "isRead": is_read,
        "isDraft": False,
    }


def test_marking_message_read_updates_all_unread_thread_messages_in_batch() -> None:
    target = _raw_graph_message("target", "conversation-1", is_read=False)

    async def request(_: object, method: str, url: str, **kwargs: object) -> object:
        if method == "GET" and url.endswith("/me/messages/target"):
            return target
        if method == "GET" and url.endswith("/me/messages"):
            params = kwargs["params"]
            assert isinstance(params, dict)
            assert "conversationId eq 'conversation-1'" in params["$filter"]
            assert "isRead eq false" in params["$filter"]
            return {"value": [{"id": "target"}, {"id": "sibling"}]}
        if method == "POST" and url.endswith("/$batch"):
            body = kwargs["json_body"]
            assert isinstance(body, dict)
            assert [request["url"] for request in body["requests"]] == [
                "/me/messages/target",
                "/me/messages/sibling",
            ]
            return {"responses": [{"id": "0", "status": 200}, {"id": "1", "status": 200}]}
        raise AssertionError(f"Unexpected request: {method} {url}")

    http = SimpleNamespace(request=AsyncMock(side_effect=request))
    client = GraphClient(http)
    account = SimpleNamespace(uuid=uuid.uuid4(), email="owner@example.com")

    message = asyncio.run(client.update_message_unread(account, "target", unread=False))

    assert message is not None
    assert message.unread is False
    assert http.request.await_count == 3


def test_marking_message_unread_only_updates_requested_message() -> None:
    target = _raw_graph_message("target", "conversation-1", is_read=True)
    http = SimpleNamespace(request=AsyncMock(side_effect=[target, b""]))
    client = GraphClient(http)
    account = SimpleNamespace(uuid=uuid.uuid4(), email="owner@example.com")

    message = asyncio.run(client.update_message_unread(account, "target", unread=True))

    assert message is not None
    assert message.unread is True
    patch_call = http.request.await_args_list[1]
    assert patch_call.args[1:] == (
        "PATCH",
        "https://graph.microsoft.com/v1.0/me/messages/target",
    )
    assert patch_call.kwargs["json_body"] == {"isRead": False}
