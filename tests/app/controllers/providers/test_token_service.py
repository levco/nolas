import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.controllers.providers.token_service import GOOGLE_TOKEN_URL, TokenService
from app.models.account import AccountProvider


def _token_service() -> TokenService:
    return TokenService(account_repo=AsyncMock(), webhook_sender=AsyncMock())


@pytest.mark.asyncio
async def test_google_refresh_uses_app_credentials(caplog: pytest.LogCaptureFixture) -> None:
    service = _token_service()
    session = MagicMock()
    response = AsyncMock()
    response.status = 200
    response.json.return_value = {"access_token": "access-token"}
    session.post.return_value.__aenter__.return_value = response
    service._get_session = AsyncMock(return_value=session)  # type: ignore[method-assign]
    app = SimpleNamespace(
        id=7,
        uuid="app-uuid",
        gmail_client_id="app-client",
        gmail_client_secret="app-secret",
    )

    with caplog.at_level(logging.INFO):
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
    assert "Using app-specific Gmail OAuth credentials" in caplog.text
    assert "app_id=7" in caplog.text
    assert "app_uuid=app-uuid" in caplog.text
    assert "client_id=app-client" in caplog.text
    assert "app-secret" not in caplog.text


@pytest.mark.asyncio
async def test_google_refresh_falls_back_to_global_credentials(caplog: pytest.LogCaptureFixture) -> None:
    service = _token_service()
    session = MagicMock()
    response = AsyncMock()
    response.status = 200
    response.json.return_value = {"access_token": "access-token"}
    session.post.return_value.__aenter__.return_value = response
    service._get_session = AsyncMock(return_value=session)  # type: ignore[method-assign]
    app = SimpleNamespace(
        id=7,
        uuid="app-uuid",
        gmail_client_id="incomplete-app-client",
        gmail_client_secret=None,
    )

    with (
        patch("app.controllers.providers.token_service.settings.google.client_id", "global-client"),
        patch("app.controllers.providers.token_service.settings.google.client_secret", "global-secret"),
    ):
        await service._refresh_google("refresh-token", app)  # type: ignore[arg-type]

    assert session.post.call_args.kwargs["data"]["client_id"] == "global-client"
    assert session.post.call_args.kwargs["data"]["client_secret"] == "global-secret"
    assert "App-specific Gmail OAuth credentials are incomplete" in caplog.text
    assert "has_client_id=True" in caplog.text
    assert "has_client_secret=False" in caplog.text


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "credentials",
    [("app-client", "app-secret"), ("app-client", None), (None, "app-secret"), (None, None)],
)
async def test_microsoft_validation_uses_app_credentials_or_global_fallback(
    credentials: tuple[str | None, str | None],
) -> None:
    service = _token_service()
    session = MagicMock()
    response = AsyncMock()
    response.status = 200
    response.json.return_value = {"access_token": "access-token", "refresh_token": "rotated-token"}
    session.post.return_value.__aenter__.return_value = response
    service._get_session = AsyncMock(return_value=session)  # type: ignore[method-assign]
    app = SimpleNamespace(
        id=7,
        uuid="app-uuid",
        microsoft_client_id=credentials[0],
        microsoft_client_secret=credentials[1],
    )
    use_app = all(credentials)

    with (
        patch("app.controllers.providers.token_service.settings.microsoft.client_id", "global-client"),
        patch("app.controllers.providers.token_service.settings.microsoft.client_secret", "global-secret"),
    ):
        result = await service.validate_refresh_token(AccountProvider.microsoft, "refresh-token", app)

    assert result == {"access_token": "access-token", "refresh_token": "rotated-token"}
    payload = session.post.call_args.kwargs["data"]
    assert payload["client_id"] == ("app-client" if use_app else "global-client")
    assert payload["client_secret"] == ("app-secret" if use_app else "global-secret")
    assert payload["refresh_token"] == "refresh-token"
    assert payload["grant_type"] == "refresh_token"


@pytest.mark.asyncio
async def test_microsoft_access_token_refresh_passes_app_and_saves_rotated_token() -> None:
    service = _token_service()
    app = SimpleNamespace(microsoft_client_id="app-client", microsoft_client_secret="app-secret")
    account = SimpleNamespace(
        provider=AccountProvider.microsoft, credentials="encrypted-old", provider_context={}, app=app
    )
    service._refresh_microsoft = AsyncMock(  # type: ignore[method-assign]
        return_value={"access_token": "access-token", "refresh_token": "rotated-token", "expires_in": 3600}
    )

    with (
        patch("app.controllers.providers.token_service.PasswordUtils.decrypt_password", return_value="old-token"),
        patch(
            "app.controllers.providers.token_service.PasswordUtils.encrypt_password",
            side_effect=lambda token: f"encrypted:{token}",
        ),
    ):
        result = await service._refresh_access_token(account)  # type: ignore[arg-type]

    assert result == "access-token"
    service._refresh_microsoft.assert_awaited_once_with(app, "old-token")
    update = service._account_repo.update.call_args.args[1]
    assert update["credentials"] == "encrypted:rotated-token"
    assert update["provider_context"]["access_token"] == "encrypted:access-token"
