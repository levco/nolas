import asyncio
import base64
import json
import sys
import types
import uuid
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

from app.controllers.providers.exceptions import ProviderError

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


def _batch_part(content_id: str, status_code: int, body: str) -> str:
    status_text = "OK" if 200 <= status_code < 300 else "Not Found"
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
