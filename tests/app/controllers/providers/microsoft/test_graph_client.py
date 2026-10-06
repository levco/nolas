import asyncio
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.controllers.providers.base import ListMessagesParams, ListThreadsParams
from app.controllers.providers.exceptions import ProviderError

from app.controllers.providers.microsoft.graph_client import GraphClient


def test_create_subscription_includes_updates_and_immutable_ids() -> None:
    http = SimpleNamespace(
        request=AsyncMock(return_value={"id": "subscription-1", "expirationDateTime": "2026-07-30T12:00:00Z"})
    )
    client = GraphClient(http)
    account = SimpleNamespace(uuid=uuid.uuid4(), email="owner@example.com")

    asyncio.run(client.create_subscription(account, "https://example.com/notifications", "secret"))

    request = http.request.await_args
    assert request.kwargs["json_body"]["changeType"] == "created,updated"
    assert request.kwargs["headers"] == {"Prefer": 'IdType="ImmutableId"'}


def test_list_threads_requests_newest_messages_first() -> None:
    http = SimpleNamespace(request=AsyncMock(return_value={"value": []}))
    client = GraphClient(http)
    account = SimpleNamespace(uuid=uuid.uuid4(), email="owner@example.com")

    asyncio.run(client.list_threads(account, ListThreadsParams(limit=20)))

    query = http.request.await_args.kwargs["params"]
    assert query["$orderby"] == "receivedDateTime desc"
    assert query["$filter"] == "receivedDateTime ge 1900-01-01T00:00:00Z and isDraft eq false"


@pytest.mark.parametrize("native_query", ["matt", "subject:invoice", "from:matt@example.com"])
def test_list_threads_routes_mailbox_text_to_search(native_query: str) -> None:
    http = SimpleNamespace(request=AsyncMock(return_value={"value": []}))
    client = GraphClient(http)
    account = SimpleNamespace(uuid=uuid.uuid4(), email="owner@example.com")

    asyncio.run(client.list_threads(account, ListThreadsParams(search_query_native=native_query)))

    query = http.request.await_args.kwargs["params"]
    assert query["$search"] == f'"{native_query}"'
    assert "$filter" not in query
    assert "$orderby" not in query


@pytest.mark.parametrize("prefix", ["", "$filter="])
def test_list_messages_preserves_native_crawling_filter(prefix: str) -> None:
    http = SimpleNamespace(request=AsyncMock(return_value={"value": []}))
    client = GraphClient(http)
    account = SimpleNamespace(uuid=uuid.uuid4(), email="owner@example.com")
    native_filter = "receivedDateTime ge 2024-01-01T00:00:00Z"

    asyncio.run(client.list_messages(account, ListMessagesParams(search_query_native=prefix + native_filter)))

    query = http.request.await_args.kwargs["params"]
    assert query["$filter"] == native_filter
    assert "$search" not in query


def test_list_threads_accepts_nylas_native_filter() -> None:
    http = SimpleNamespace(request=AsyncMock(return_value={"value": []}))
    client = GraphClient(http)
    account = SimpleNamespace(uuid=uuid.uuid4(), email="owner@example.com")
    native_filter = "from/emailAddress/address eq 'matt@example.com'"

    asyncio.run(client.list_threads(account, ListThreadsParams(search_query_native="$filter=" + native_filter)))

    query = http.request.await_args.kwargs["params"]
    assert query["$filter"] == f"receivedDateTime ge 1900-01-01T00:00:00Z and {native_filter}"
    assert "$search" not in query


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
            assert "dependsOn" not in body["requests"][0]
            assert body["requests"][1]["dependsOn"] == ["0"]
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


def test_sequential_batch_retries_siblings_skipped_after_deleted_message() -> None:
    http = SimpleNamespace(
        request=AsyncMock(
            side_effect=[
                {"responses": [{"id": "1", "status": 424}, {"id": "0", "status": 404}]},
                {"responses": [{"id": "0", "status": 200}]},
            ]
        )
    )
    client = GraphClient(http)
    account = SimpleNamespace(uuid=uuid.uuid4(), email="owner@example.com")

    asyncio.run(client._mark_messages_read(account, ["deleted", "sibling"]))

    retry = http.request.await_args.kwargs["json_body"]["requests"]
    assert len(retry) == 1
    assert retry[0]["url"] == "/me/messages/sibling"
    assert "dependsOn" not in retry[0]


def test_sequential_batch_propagates_rate_limit_without_retrying_dependencies() -> None:
    from app.controllers.providers.exceptions import ProviderRateLimitError

    http = SimpleNamespace(
        request=AsyncMock(return_value={"responses": [{"id": "1", "status": 424}, {"id": "0", "status": 429}]})
    )
    client = GraphClient(http)
    account = SimpleNamespace(uuid=uuid.uuid4(), email="owner@example.com")

    with pytest.raises(ProviderRateLimitError):
        asyncio.run(client._mark_messages_read(account, ["throttled", "sibling"]))
    assert http.request.await_count == 1


@pytest.mark.parametrize("last_status", [200, 404])
def test_sequential_batch_retries_multiple_deleted_messages_within_limit(last_status: int) -> None:
    http = SimpleNamespace(
        request=AsyncMock(
            side_effect=[
                {"responses": [{"id": "0", "status": 404}, {"id": "1", "status": 424}, {"id": "2", "status": 424}]},
                {"responses": [{"id": "0", "status": 404}, {"id": "1", "status": 424}]},
                {"responses": [{"id": "0", "status": last_status}]},
            ]
        )
    )
    client = GraphClient(http)
    account = SimpleNamespace(uuid=uuid.uuid4(), email="owner@example.com")

    asyncio.run(client._mark_messages_read(account, ["deleted-1", "deleted-2", "last"]))

    assert http.request.await_count == 3
    assert [
        [request["url"] for request in call.kwargs["json_body"]["requests"]] for call in http.request.await_args_list
    ] == [
        ["/me/messages/deleted-1", "/me/messages/deleted-2", "/me/messages/last"],
        ["/me/messages/deleted-2", "/me/messages/last"],
        ["/me/messages/last"],
    ]


def test_sequential_batch_stops_after_three_attempts_with_pending_messages() -> None:
    http = SimpleNamespace(
        request=AsyncMock(
            side_effect=[
                {"responses": [{"id": str(i), "status": 404 if i == 0 else 424} for i in range(size)]}
                for size in [4, 3, 2]
            ]
        )
    )
    client = GraphClient(http)
    account = SimpleNamespace(uuid=uuid.uuid4(), email="owner@example.com")

    with pytest.raises(ProviderError, match="pending messages after 3 attempts") as error:
        asyncio.run(client._mark_messages_read(account, ["deleted-1", "deleted-2", "deleted-3", "last"]))

    assert error.value.status_code == 502
    assert http.request.await_count == 3


def test_sequential_batch_rejects_retry_without_progress() -> None:
    http = SimpleNamespace(request=AsyncMock(return_value={"responses": [{"id": "0", "status": 424}]}))
    client = GraphClient(http)
    account = SimpleNamespace(uuid=uuid.uuid4(), email="owner@example.com")

    with pytest.raises(ProviderError, match="no progress"):
        asyncio.run(client._mark_messages_read(account, ["message-1"]))

    assert http.request.await_count == 1
