from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from app.controllers.grant.custom_auth_controller import CustomAuthController
from app.models.account import AccountProvider, AccountStatus


def _make_controller() -> tuple[CustomAuthController, AsyncMock, AsyncMock, AsyncMock]:
    account_repo = AsyncMock()
    token_service = AsyncMock()
    subscription_manager = AsyncMock()
    controller = CustomAuthController(account_repo, token_service, subscription_manager)
    return controller, account_repo, token_service, subscription_manager


class TestCustomAuthController:
    @pytest.mark.asyncio
    async def test_google_reauth_discards_old_history_cursor(self) -> None:
        controller, account_repo, token_service, subscription_manager = _make_controller()
        app = SimpleNamespace(id=7)
        account = SimpleNamespace(
            provider=AccountProvider.google,
            provider_context={
                "history_id": "old-history",
                "watch_expiration": 123,
                "access_token": "old-access-token",
            },
        )
        token_service.validate_refresh_token.return_value = {"access_token": "access-token"}
        account_repo.get_by_app_and_email.return_value = account
        controller._fetch_account_email = AsyncMock(return_value="User@example.com")  # type: ignore[method-assign]

        with patch("app.controllers.grant.custom_auth_controller.PasswordUtils.encrypt_password", return_value="encrypted"):
            await controller.create_grant_from_refresh_token(app, AccountProvider.google, "refresh-token")

        update = account_repo.update.await_args.args[1]
        assert update["provider_context"] == {"watch_expiration": 123}
        assert update["status"] == AccountStatus.active
        subscription_manager.ensure_subscription.assert_awaited_once_with(account)

    @pytest.mark.asyncio
    async def test_google_refresh_token_update_discards_old_history_cursor(self) -> None:
        controller, account_repo, token_service, subscription_manager = _make_controller()
        account = SimpleNamespace(
            uuid="grant-id",
            email="user@example.com",
            provider=AccountProvider.google,
            provider_context={"history_id": "old-history", "watch_expiration": 123},
        )
        token_service.validate_refresh_token.return_value = {"access_token": "access-token"}
        controller._fetch_account_email = AsyncMock(return_value="user@example.com")  # type: ignore[method-assign]

        with patch("app.controllers.grant.custom_auth_controller.PasswordUtils.encrypt_password", return_value="encrypted"):
            await controller.update_grant_refresh_token(account, "refresh-token")

        update = account_repo.update.await_args.args[1]
        assert update["provider_context"] == {"watch_expiration": 123}
        subscription_manager.ensure_subscription.assert_awaited_once_with(account)

    @pytest.mark.asyncio
    async def test_microsoft_refresh_token_update_preserves_provider_context(self) -> None:
        controller, account_repo, token_service, _ = _make_controller()
        account = SimpleNamespace(
            uuid="grant-id",
            email="user@example.com",
            provider=AccountProvider.microsoft,
            provider_context={"subscription_id": "sub-id", "history_id": "unrelated-value"},
        )
        token_service.validate_refresh_token.return_value = {"access_token": "access-token"}
        controller._fetch_account_email = AsyncMock(return_value="user@example.com")  # type: ignore[method-assign]

        with patch("app.controllers.grant.custom_auth_controller.PasswordUtils.encrypt_password", return_value="encrypted"):
            await controller.update_grant_refresh_token(account, "refresh-token")

        update = account_repo.update.await_args.args[1]
        assert update["provider_context"] == {
            "subscription_id": "sub-id",
            "history_id": "unrelated-value",
        }
