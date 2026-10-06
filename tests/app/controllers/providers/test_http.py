from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.controllers.providers.exceptions import ProviderRateLimitError
from app.controllers.providers.http import AuthorizedHttpClient


@pytest.mark.asyncio
@pytest.mark.parametrize("statuses", [(200,), (401, 200), (429,)])
async def test_capacity_covers_response_body_and_each_auth_retry(statuses) -> None:
    active = False
    events = []

    @asynccontextmanager
    async def capacity(account):
        nonlocal active
        assert not active
        active = True
        events.append("acquire")
        try:
            yield
        finally:
            active = False
            events.append("release")

    async def read_body():
        assert active
        events.append("body")
        return {"ok": True}

    @asynccontextmanager
    async def response(status):
        assert active
        events.append("request")
        yield SimpleNamespace(
            status=status,
            headers={"Content-Type": "application/json"},
            json=read_body,
            text=AsyncMock(return_value="rate limited"),
        )

    token_service = AsyncMock()
    token_service.get_access_token.return_value = "access-token"
    limiter = SimpleNamespace(acquire=capacity)
    client = AuthorizedHttpClient(token_service, timeout=30, concurrency_limiter=limiter)
    session = SimpleNamespace(request=MagicMock(side_effect=[response(status) for status in statuses]))
    client._get_session = AsyncMock(return_value=session)
    account = SimpleNamespace()

    if statuses == (429,):
        with pytest.raises(ProviderRateLimitError):
            await client.request(account, "GET", "https://graph.microsoft.com/v1.0/me")
    else:
        assert await client.request(account, "GET", "https://graph.microsoft.com/v1.0/me") == {"ok": True}
    assert not active
    assert events.count("acquire") == len(statuses)
    assert events.count("release") == len(statuses)
    if statuses == (401, 200):
        assert events == ["acquire", "request", "release", "acquire", "request", "body", "release"]
        token_service.get_access_token.assert_awaited_with(account, force_refresh=True)


@pytest.mark.asyncio
async def test_pre_authenticated_upload_still_acquires_capacity() -> None:
    limiter = MagicMock()
    response = AsyncMock(status=204)
    response.read.return_value = b""
    session = MagicMock()
    session.request.return_value.__aenter__.return_value = response
    token_service = AsyncMock()
    client = AuthorizedHttpClient(token_service, timeout=30, concurrency_limiter=limiter)
    client._get_session = AsyncMock(return_value=session)
    account = SimpleNamespace()

    await client.request(account, "PUT", "https://upload.example.com", data=b"chunk", include_auth=False)

    limiter.acquire.assert_called_once_with(account)
    token_service.get_access_token.assert_not_awaited()
    assert "Authorization" not in session.request.call_args.kwargs["headers"]
