from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from email.utils import format_datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, Mock, patch

import pytest

from app.controllers.providers.exceptions import ProviderError, ProviderRateLimitError
from app.controllers.providers.http import AuthorizedHttpClient, _retry_after_seconds


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


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "status,body",
    [
        (403, "Quota exceeded for quota metric 'Total Query Cost' and limit 'Units per minute per user'"),
        (403, '{"error":{"errors":[{"reason":"userRateLimitExceeded"}]}}'),
        (429, "Too many requests"),
    ],
)
async def test_quota_responses_are_retryable_without_expiring_the_grant(status: int, body: str) -> None:
    token_service = AsyncMock()
    client = AuthorizedHttpClient(token_service, timeout=30)
    response = Mock(status=status, headers={"Retry-After": "900"}, text=AsyncMock(return_value=body))
    with pytest.raises(ProviderRateLimitError) as error:
        await client._handle_response(Mock(), response, True)
    assert error.value.retry_after_seconds == 900
    token_service.handle_auth_failure.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("body", ['{"reason":"insufficientPermissions"}', "Permission denied: generate a separate app"])
async def test_forbidden_permissions_are_not_treated_as_a_quota_error(body: str) -> None:
    client = AuthorizedHttpClient(AsyncMock(), timeout=30)
    response = Mock(status=403, headers={}, text=AsyncMock(return_value=body))
    with pytest.raises(ProviderError) as error:
        await client._handle_response(Mock(), response, True)
    assert not isinstance(error.value, ProviderRateLimitError)
    assert error.value.status_code == 403


@pytest.mark.parametrize("value,expected", [(None, None), ("120", 120), ("900", 900), ("invalid", None), ("-1", 0)])
def test_retry_after_seconds_or_invalid_header(value: str | None, expected: int | None) -> None:
    assert _retry_after_seconds(value) == expected


@pytest.mark.parametrize("offset,expected", [(900, 900), (-60, 0)])
def test_retry_after_http_date(offset: int, expected: int) -> None:
    now = datetime(2026, 10, 29, tzinfo=UTC)
    header = format_datetime(now + timedelta(seconds=offset), usegmt=True)
    with patch("app.controllers.providers.http.datetime") as clock:
        clock.now.return_value = now
        assert _retry_after_seconds(header) == expected
