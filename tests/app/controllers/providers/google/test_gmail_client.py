import asyncio
import base64
import json
import sys
import types
import uuid
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

from app.controllers.providers.base import ListThreadsParams
from app.controllers.providers.exceptions import ProviderError, ProviderRateLimitError

if "aiohttp" not in sys.modules:
    aiohttp_stub = types.ModuleType("aiohttp")

    class _ClientTimeout:
        def __init__(self, total: int | None = None) -> None:
            self.total = total

    class _ClientSession:
        closed = False

    class _ClientResponse:
        pass

    aiohttp_stub.ClientTimeout = _ClientTimeout
    aiohttp_stub.ClientSession = _ClientSession
    aiohttp_stub.ClientResponse = _ClientResponse
    sys.modules["aiohttp"] = aiohttp_stub

from app.controllers.providers.google.gmail_client import GMAIL_BATCH_BASE, GmailClient


def _b64(text: str) -> str:
    return base64.urlsafe_b64encode(text.encode()).decode()


def _account() -> Any:
    return SimpleNamespace(uuid=uuid.uuid4(), email="owner@example.com")


def _raw_message(message_id: str) -> dict[str, Any]:
    return {
        "id": message_id,
        "threadId": f"thread-{message_id}",
        "labelIds": ["INBOX"],
        "snippet": f"snippet-{message_id}",
        "internalDate": "1717920000000",
        "payload": {
            "headers": [
                {"name": "Subject", "value": f"subject-{message_id}"},
                {"name": "From", "value": "Sender <sender@example.com>"},
            ],
            "parts": [
                {
                    "mimeType": "text/plain",
                    "body": {"data": _b64(f"body-{message_id}")},
                }
            ],
        },
    }


def _raw_thread_message(message_id: str, thread_id: str, starred: bool = False, unread: bool = True) -> dict[str, Any]:
    labels = ["INBOX"]
    if starred:
        labels.append("STARRED")
    if unread:
        labels.append("UNREAD")
    return {
        "id": message_id,
        "threadId": thread_id,
        "labelIds": labels,
        "snippet": f"snippet-{message_id}",
        "internalDate": "1717920000000",
        "payload": {
            "headers": [
                {"name": "Subject", "value": f"subject-{thread_id}"},
                {"name": "From", "value": "Sender <sender@example.com>"},
                {"name": "To", "value": "Receiver <receiver@example.com>"},
            ],
            "parts": [
                {
                    "mimeType": "text/plain",
                    "body": {"data": _b64(f"body-{message_id}")},
                }
            ],
        },
    }


def _batch_part(content_id: str, status_code: int, body: str) -> str:
    if status_code == 404:
        status_text = "Not Found"
    elif status_code == 429:
        status_text = "Too Many Requests"
    elif 200 <= status_code < 300:
        status_text = "OK"
    else:
        status_text = "Error"
    return (
        "Content-Type: application/http\r\n"
        f"Content-ID: <{content_id}>\r\n"
        "\r\n"
        f"HTTP/1.1 {status_code} {status_text}\r\n"
        "Content-Type: application/json; charset=UTF-8\r\n"
        "\r\n"
        f"{body}\r\n"
    )


def _batch_response(parts: list[str], boundary: str = "batch_response") -> bytes:
    segments = [f"--{boundary}\r\n{part}" for part in parts]
    segments.append(f"--{boundary}--\r\n")
    return "".join(segments).encode()


class TestGmailClientBatchHydration:
    def test_hydrates_messages_via_batch_and_preserves_listing_order(self) -> None:
        http = SimpleNamespace(request=AsyncMock())
        http.request.return_value = _batch_response(
            [
                _batch_part("m1", 200, json.dumps(_raw_message("m1"))),
                _batch_part("m2", 200, json.dumps(_raw_message("m2"))),
            ]
        )
        client = GmailClient(http)

        messages = asyncio.run(client._hydrate_messages(_account(), ["m2", "m1"], include_headers=False))

        assert [message.id for message in messages] == ["m2", "m1"]
        assert http.request.await_count == 1
        call = http.request.await_args
        assert call.args[2] == GMAIL_BATCH_BASE
        request_body = call.kwargs["data"].decode()
        assert "GET /gmail/v1/users/me/messages/m1?format=full HTTP/1.1" in request_body
        assert "GET /gmail/v1/users/me/messages/m2?format=full HTTP/1.1" in request_body

    def test_skips_404_subresponses_in_batch(self) -> None:
        http = SimpleNamespace(request=AsyncMock())
        http.request.return_value = _batch_response(
            [
                _batch_part("missing", 404, '{"error":{"message":"not found"}}'),
                _batch_part("m1", 200, json.dumps(_raw_message("m1"))),
            ]
        )
        client = GmailClient(http)

        messages = asyncio.run(client._hydrate_messages(_account(), ["missing", "m1"], include_headers=False))

        assert [message.id for message in messages] == ["m1"]
        assert http.request.await_count == 1

    def test_falls_back_to_individual_fetch_when_batch_fails(self) -> None:
        async def request(_: Any, method: str, url: str, **kwargs: Any) -> Any:
            if method == "POST" and url == GMAIL_BATCH_BASE:
                raise ProviderError("batch unavailable")
            assert method == "GET"
            assert kwargs["params"] == {"format": "full"}
            message_id = url.rsplit("/", 1)[-1]
            return _raw_message(message_id)

        http = SimpleNamespace(request=AsyncMock(side_effect=request))
        client = GmailClient(http)

        messages = asyncio.run(client._hydrate_messages(_account(), ["m1", "m2"], include_headers=False))

        assert [message.id for message in messages] == ["m1", "m2"]
        assert http.request.await_count == 3
        assert http.request.await_args_list[0].args[2] == GMAIL_BATCH_BASE

    def test_fetches_rate_limited_subresponses_individually(self) -> None:
        async def request(_: Any, method: str, url: str, **kwargs: Any) -> Any:
            if method == "POST" and url == GMAIL_BATCH_BASE:
                return _batch_response(
                    [
                        _batch_part("m1", 429, '{"error":{"message":"rate limited"}}'),
                        _batch_part("m2", 200, json.dumps(_raw_message("m2"))),
                    ]
                )
            assert method == "GET"
            assert kwargs["params"] == {"format": "full"}
            message_id = url.rsplit("/", 1)[-1]
            return _raw_message(message_id)

        http = SimpleNamespace(request=AsyncMock(side_effect=request))
        client = GmailClient(http)

        messages = asyncio.run(client._hydrate_messages(_account(), ["m1", "m2"], include_headers=False))

        assert [message.id for message in messages] == ["m1", "m2"]
        assert http.request.await_count == 2
        assert http.request.await_args_list[0].args[2] == GMAIL_BATCH_BASE

    def test_falls_back_when_batch_envelope_is_rate_limited(self) -> None:
        async def request(_: Any, method: str, url: str, **kwargs: Any) -> Any:
            if method == "POST" and url == GMAIL_BATCH_BASE:
                raise ProviderRateLimitError("envelope rate limited")
            assert method == "GET"
            assert kwargs["params"] == {"format": "full"}
            message_id = url.rsplit("/", 1)[-1]
            return _raw_message(message_id)

        http = SimpleNamespace(request=AsyncMock(side_effect=request))
        client = GmailClient(http)

        messages = asyncio.run(client._hydrate_messages(_account(), ["m1", "m2"], include_headers=False))

        assert [message.id for message in messages] == ["m1", "m2"]
        assert http.request.await_count == 3
        assert http.request.await_args_list[0].args[2] == GMAIL_BATCH_BASE

    def test_normalizes_response_content_id_before_rate_limit_fallback(self) -> None:
        async def request(_: Any, method: str, url: str, **kwargs: Any) -> Any:
            if method == "POST" and url == GMAIL_BATCH_BASE:
                return _batch_response(
                    [
                        _batch_part("response-m1+abc", 429, '{"error":{"message":"rate limited"}}'),
                    ]
                )
            assert method == "GET"
            assert kwargs["params"] == {"format": "full"}
            message_id = url.rsplit("/", 1)[-1]
            assert message_id == "m1"
            return _raw_message(message_id)

        http = SimpleNamespace(request=AsyncMock(side_effect=request))
        client = GmailClient(http)

        messages = asyncio.run(client._hydrate_messages(_account(), ["m1"], include_headers=False))

        assert [message.id for message in messages] == ["m1"]
        assert http.request.await_count == 2
        assert http.request.await_args_list[0].args[2] == GMAIL_BATCH_BASE


class TestGmailClientThreads:
    def test_lists_threads_from_gmail_threads_endpoint(self) -> None:
        async def request(_: Any, method: str, url: str, **kwargs: Any) -> Any:
            assert method == "GET"
            if url.endswith("/threads"):
                assert kwargs["params"]["maxResults"] == 2
                assert "in:INBOX" in kwargs["params"]["q"]
                return {"threads": [{"id": "t1"}, {"id": "t2"}], "nextPageToken": "cursor-2"}
            if url.endswith("/threads/t1"):
                first = _raw_thread_message("m1", "t1", starred=False, unread=True)
                second = _raw_thread_message("m2", "t1", starred=True, unread=False)
                second["internalDate"] = "1717930000000"
                second["payload"]["parts"].append(
                    {
                        "filename": "invoice.pdf",
                        "mimeType": "application/pdf",
                        "body": {"attachmentId": "att-1", "size": 12},
                    }
                )
                return {"id": "t1", "messages": [first, second]}
            if url.endswith("/threads/t2"):
                return {"id": "t2", "messages": [_raw_thread_message("m3", "t2", starred=False, unread=False)]}
            raise AssertionError(f"Unexpected request: {url}")

        http = SimpleNamespace(request=AsyncMock(side_effect=request))
        client = GmailClient(http)

        result = asyncio.run(client.list_threads(_account(), ListThreadsParams(limit=2, in_="INBOX")))

        assert [thread.id for thread in result.threads] == ["t1", "t2"]
        assert result.next_cursor == "cursor-2"
        assert result.threads[0].latest_message_received_date == 1717930000
        assert result.threads[0].earliest_message_date == 1717920000
        assert result.threads[0].has_attachments is True
        assert result.threads[0].starred is True
        # Gmail provides an efficient native any-message unread thread filter.
        assert result.threads[0].unread is True
        assert result.threads[0].message_ids == ["m2", "m1"]
        assert result.threads[0].latest_draft_or_message.id == "m2"


class TestGmailClientUpdateUnread:
    def test_marks_whole_thread_read(self) -> None:
        raw = _raw_thread_message("m1", "t1", unread=True)
        http = SimpleNamespace(request=AsyncMock(side_effect=[raw, {"id": "t1"}]))
        client = GmailClient(http)

        message = asyncio.run(client.update_message_unread(_account(), "m1", unread=False))

        assert message is not None
        assert message.unread is False
        modify_call = http.request.await_args_list[1]
        assert modify_call.args[1:] == ("POST", "https://gmail.googleapis.com/gmail/v1/users/me/threads/t1/modify")
        assert modify_call.kwargs["json_body"] == {"removeLabelIds": ["UNREAD"]}

    def test_marks_only_requested_message_unread(self) -> None:
        raw = _raw_thread_message("m1", "t1", unread=False)
        http = SimpleNamespace(request=AsyncMock(side_effect=[raw, {"id": "m1"}]))
        client = GmailClient(http)

        message = asyncio.run(client.update_message_unread(_account(), "m1", unread=True))

        assert message is not None
        assert message.unread is True
        modify_call = http.request.await_args_list[1]
        assert modify_call.args[1:] == (
            "POST",
            "https://gmail.googleapis.com/gmail/v1/users/me/messages/m1/modify",
        )
        assert modify_call.kwargs["json_body"] == {"addLabelIds": ["UNREAD"]}
