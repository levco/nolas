import asyncio
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

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
    assert request.kwargs["json_body"]["changeType"] == "created,updated,deleted"
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


@pytest.mark.parametrize(
    ("received_after", "received_before", "date_filter"),
    [
        (1700000000, None, "receivedDateTime ge 2023-11-14T22:13:20Z"),
        (None, 1710000000, "receivedDateTime le 2024-03-09T16:00:00Z"),
        (
            1700000000,
            1710000000,
            "receivedDateTime ge 2023-11-14T22:13:20Z and receivedDateTime le 2024-03-09T16:00:00Z",
        ),
    ],
)
def test_list_messages_places_ordered_dates_before_other_filters(
    received_after: int | None, received_before: int | None, date_filter: str
) -> None:
    http = SimpleNamespace(request=AsyncMock(return_value={"value": []}))
    client = GraphClient(http)
    account = SimpleNamespace(uuid=uuid.uuid4(), email="owner@example.com")

    asyncio.run(
        client.list_messages(
            account,
            ListMessagesParams(
                thread_id="conversation-1", received_after=received_after, received_before=received_before
            ),
        )
    )

    query = http.request.await_args.kwargs["params"]
    assert query["$orderby"] == "receivedDateTime desc"
    assert query["$filter"] == f"{date_filter} and isDraft eq false and conversationId eq 'conversation-1'"


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


def test_index_refresh_fetches_every_conversation_page_with_compact_fields() -> None:
    newest = _raw_graph_message("newest", "conversation", is_read=True)
    newest["parentFolderId"] = "sent-id"
    older = {**_raw_graph_message("older", "conversation", is_read=False), "parentFolderId": "inbox-id"}
    http = Mock(
        request=AsyncMock(
            side_effect=[
                {"value": [newest], "@odata.nextLink": "https://graph.microsoft.com/v1.0/me/messages?$skip=100"},
                {"value": [older, {"id": "draft", "isDraft": True}]},
            ]
        )
    )
    client = GraphClient(http)
    account = Mock(uuid=uuid.uuid4())
    messages = asyncio.run(client.get_thread_messages(account, "conversation"))
    assert [message.id for message in messages] == ["newest", "older"]
    assert {folder for message in messages for folder in message.folders} == {"inbox-id", "sent-id"}
    first, second = http.request.await_args_list
    assert "body" not in first.kwargs["params"]["$select"].split(",")
    assert "$expand" not in first.kwargs["params"]
    assert second.args[2] == "https://graph.microsoft.com/v1.0/me/messages?$skip=100"
    assert second.kwargs["headers"] == {"Prefer": 'IdType="ImmutableId"'}


def test_index_delta_requests_only_metadata_and_reuses_provider_cursor() -> None:
    http = Mock(request=AsyncMock(return_value={"value": [], "@odata.deltaLink": "next-delta"}))
    client = GraphClient(http)
    account = Mock(uuid=uuid.uuid4())
    asyncio.run(client.list_thread_changes(account, "folder/id"))
    initial = http.request.await_args
    assert initial.args[2].endswith("/me/mailFolders/folder%2Fid/messages/delta")
    assert initial.kwargs["params"] == {"$select": "id,conversationId,receivedDateTime,sentDateTime", "$top": 50}
    assert initial.kwargs["headers"] == {"Prefer": 'IdType="ImmutableId", odata.maxpagesize=50'}
    asyncio.run(client.list_thread_changes(account, "folder/id", "https://graph.microsoft.com/v1.0/opaque-delta"))
    assert http.request.await_args.args[2] == "https://graph.microsoft.com/v1.0/opaque-delta"
    assert http.request.await_args.kwargs["params"] is None


@pytest.mark.parametrize("cursor", [None, "https://graph.microsoft.com/v1.0/opaque-delta"])
def test_index_delta_filters_dates_locally_without_the_5000_message_cap(cursor: str | None) -> None:
    entries = [
        {"id": "old", "receivedDateTime": "2024-10-28T23:59:59Z"},
        {"id": "boundary", "receivedDateTime": "2024-10-29T00:00:00Z"},
        {"id": "newer", "receivedDateTime": "2026-10-29T00:00:00Z"},
        {"id": "sent", "sentDateTime": "2024-10-29T01:00:00+01:00"},
        {"id": "old-sent", "sentDateTime": "2024-10-28T00:00:00Z"},
        {"id": "deleted", "@removed": {"reason": "deleted"}, "receivedDateTime": "2020-01-01T00:00:00Z"},
        {"id": "undated-update"},
    ]
    http = Mock(request=AsyncMock(return_value={"value": entries, "@odata.nextLink": "next-page"}))
    client = GraphClient(http)
    account = Mock(uuid=uuid.uuid4())
    response = asyncio.run(client.list_thread_changes(account, "inbox-id", cursor, received_after=1730160000))
    assert [entry["id"] for entry in response["value"]] == ["boundary", "newer", "sent", "deleted", "undated-update"]
    assert response["@odata.nextLink"] == "next-page"
    request = http.request.await_args
    if cursor:
        assert request.args[2] == cursor and request.kwargs["params"] is None
    else:
        assert "$filter" not in request.kwargs["params"]


def test_index_refresh_rejects_repeated_conversation_page_tokens() -> None:
    http = Mock(
        request=AsyncMock(return_value={"value": [], "@odata.nextLink": "https://graph.microsoft.com/repeated"})
    )
    with pytest.raises(ProviderError, match="repeated thread page"):
        asyncio.run(GraphClient(http).get_thread_messages(Mock(uuid=uuid.uuid4()), "conversation"))
    assert http.request.await_count == 2


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


@pytest.mark.parametrize("with_contact", [False, True])
def test_multi_folder_pages_keep_every_matching_thread_and_provider_cursor(with_contact: bool) -> None:
    from dataclasses import replace
    from app.controllers.providers.microsoft.query import decode_cursor

    def raw(identifier: str, folder: str) -> dict[str, object]:
        return {
            **_raw_graph_message(identifier, identifier, is_read=True),
            "parentFolderId": folder,
            "from": {"emailAddress": {"address": "contact@example.com"}},
        }

    next_link = "https://graph.microsoft.com/v1.0/me/messages?$skip=2"
    http = Mock(
        request=AsyncMock(
            side_effect=[
                {
                    "value": [raw("inbox-thread", "inbox-id"), raw("sent-thread", "sent-id")],
                    "@odata.nextLink": next_link,
                },
                {"value": [raw("another-thread", "sent-id"), raw("archive-thread", "archive-id")]},
            ]
        )
    )
    client = GraphClient(http)
    account = Mock(uuid=uuid.uuid4())
    params = ListThreadsParams(
        in_=["inbox-id", "sent-id"], any_email=["contact@example.com"] if with_contact else [], limit=2
    )
    first = asyncio.run(client.list_threads(account, params))
    second = asyncio.run(client.list_threads(account, replace(params, page_token=first.next_cursor)))

    assert [thread.id for thread in first.threads + second.threads] == ["inbox-thread", "sent-thread", "another-thread"]
    assert first.next_cursor and decode_cursor(first.next_cursor) == next_link
    assert second.next_cursor is None
    first_request, second_request = http.request.await_args_list
    query = first_request.kwargs["params"]
    assert query["$top"] == 2
    if with_contact:
        assert query["$search"] == '"participants:contact@example.com"'
    else:
        assert "(parentFolderId eq 'inbox-id' or parentFolderId eq 'sent-id')" in query["$filter"]
    assert second_request.args[2] == next_link


@pytest.mark.parametrize("has_match", [True, False])
def test_multi_folder_search_skips_empty_filtered_pages(has_match: bool) -> None:
    next_link = "https://graph.microsoft.com/v1.0/me/messages?$skip=1"
    http = Mock(
        request=AsyncMock(
            side_effect=[
                {
                    "value": [
                        {**_raw_graph_message("archive", "archive", is_read=True), "parentFolderId": "archive-id"}
                    ],
                    "@odata.nextLink": next_link,
                },
                {
                    "value": (
                        [{**_raw_graph_message("inbox", "inbox", is_read=True), "parentFolderId": "inbox-id"}]
                        if has_match
                        else []
                    ),
                },
            ]
        )
    )
    client = GraphClient(http)
    account = Mock(uuid=uuid.uuid4())
    params = ListThreadsParams(in_=["inbox-id", "sent-id"], search_query_native="subject:report", limit=1)

    result = asyncio.run(client.list_threads(account, params))
    assert [thread.id for thread in result.threads] == (["inbox"] if has_match else [])
    assert result.next_cursor is None
    assert http.request.await_args_list[1].args[2] == next_link


@pytest.mark.parametrize("pages", [["page-2", "page-2"], ["page-2", "page-3", "page-2"]])
def test_multi_folder_search_rejects_repeated_empty_page_tokens(pages: list[str]) -> None:
    http = Mock(
        request=AsyncMock(
            side_effect=[{"value": [], "@odata.nextLink": f"https://graph.microsoft.com/{page}"} for page in pages]
        )
    )
    client = GraphClient(http)
    account = Mock(uuid=uuid.uuid4())
    params = ListThreadsParams(in_=["inbox-id", "sent-id"], any_email=["contact@example.com"])

    with pytest.raises(ProviderError, match="repeated thread page"):
        asyncio.run(client.list_threads(account, params))

    assert http.request.await_count == len(pages)
