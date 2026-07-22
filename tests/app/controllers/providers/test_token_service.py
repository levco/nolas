from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.controllers.providers.token_service import GOOGLE_TOKEN_URL, TokenService


def _token_service() -> TokenService:
    return TokenService(account_repo=AsyncMock(), webhook_sender=AsyncMock())


@pytest.mark.asyncio
async def test_google_refresh_uses_app_credentials() -> None:
    service = _token_service()
    session = MagicMock()
    response = AsyncMock()
    response.status = 200
    response.json.return_value = {"access_token": "access-token"}
    session.post.return_value.__aenter__.return_value = response
    service._get_session = AsyncMock(return_value=session)  # type: ignore[method-assign]
    app = SimpleNamespace(gmail_client_id="app-client", gmail_client_secret="app-secret")

    result = await service._refresh_google("refresh-token", app)  # type: ignore[arg-type]

    assert result == {"access_token": "access-token"}
    session.post.assert_called_once_with(
        GOOGLE_TOKEN_URL,
        data={
            "client_id": "app-client",
            "client_secret": "app-secret",
            "refresh_token": "refresh-token",
            "grant_type": "refresh_token",
        },
    )


@pytest.mark.asyncio
async def test_google_refresh_falls_back_to_global_credentials() -> None:
    service = _token_service()
    session = MagicMock()
    response = AsyncMock()
    response.status = 200
    response.json.return_value = {"access_token": "access-token"}
    session.post.return_value.__aenter__.return_value = response
    service._get_session = AsyncMock(return_value=session)  # type: ignore[method-assign]
    app = SimpleNamespace(gmail_client_id="incomplete-app-client", gmail_client_secret=None)

    with patch("app.controllers.providers.token_service.settings.google.client_id", "global-client"), patch(
        "app.controllers.providers.token_service.settings.google.client_secret", "global-secret"
    ):
        await service._refresh_google("refresh-token", app)  # type: ignore[arg-type]

    assert session.post.call_args.kwargs["data"]["client_id"] == "global-client"
    assert session.post.call_args.kwargs["data"]["client_secret"] == "global-secret"
