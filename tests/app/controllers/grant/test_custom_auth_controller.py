from unittest.mock import AsyncMock, patch

import pytest

from app.controllers.grant.custom_auth_controller import CustomAuthController
from app.models.account import Account, AccountProvider, AccountStatus
from app.models.app import App


def _make_controller() -> tuple[CustomAuthController, AsyncMock, AsyncMock, AsyncMock]:
    account_repo = AsyncMock()
    token_service = AsyncMock()
    subscription_manager = AsyncMock()
    controller = CustomAuthController(account_repo, token_service, subscription_manager)
    return controller, account_repo, token_service, subscription_manager


class TestCustomAuthController:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("reconnect_method", ["create_grant_from_refresh_token", "update_grant_refresh_token"])
    async def test_microsoft_reconnect_survives_subscription_account_reload(self, reconnect_method: str) -> None:
        controller, account_repo, token_service, subscription_manager = _make_controller()
        app = App(id=7)
        account = Account(
            id=9,
            app=app,
            email="user@example.com",
            provider=AccountProvider.microsoft,
            status=AccountStatus.inactive,
            credentials="encrypted-old-token",
            provider_context={"subscription_id": "sub-id", "access_token": "stale-token"},
        )
        persisted = {
            "status": account.status,
            "credentials": account.credentials,
            "provider_context": account.provider_context,
        }

        async def update(current: Account, values: dict[str, object], *, do_commit: bool) -> Account:
            for key, value in values.items():
                setattr(current, key, value)
            return current

        async def flush() -> None:
            persisted.update(
                status=account.status, credentials=account.credentials, provider_context=account.provider_context
            )

        async def reload_for_subscription(current: Account) -> None:
            # Microsoft token refresh reloads the account, discarding unflushed changes.
            for key, value in persisted.items():
                setattr(current, key, value)

        account_repo.update.side_effect = update
        account_repo.flush.side_effect = flush
        account_repo.get_by_app_and_email.return_value = account
        token_service.validate_refresh_token.return_value = {
            "access_token": "new-access-token",
            "refresh_token": "rotated-refresh-token",
        }
        controller._fetch_account_email = AsyncMock(return_value=account.email)  # type: ignore[method-assign]
        subscription_manager.ensure_subscription.side_effect = reload_for_subscription

        with patch(
            "app.controllers.grant.custom_auth_controller.PasswordUtils.encrypt_password",
            side_effect=lambda token: f"encrypted:{token}",
        ):
            if reconnect_method == "create_grant_from_refresh_token":
                result = await controller.create_grant_from_refresh_token(app, AccountProvider.microsoft, "new-token")
            else:
                result = await controller.update_grant_refresh_token(app, account, "new-token")

        assert result is account
        assert account.status == AccountStatus.active
        assert account.credentials == "encrypted:rotated-refresh-token"
        assert account.provider_context == {"subscription_id": "sub-id"}

    @pytest.mark.asyncio
    async def test_google_reauth_discards_old_history_cursor(self) -> None:
        controller, account_repo, token_service, subscription_manager = _make_controller()
        app = App(id=7)
        account = Account(
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

        with patch(
            "app.controllers.grant.custom_auth_controller.PasswordUtils.encrypt_password", return_value="encrypted"
        ):
            await controller.create_grant_from_refresh_token(app, AccountProvider.google, "refresh-token")

        update = account_repo.update.await_args.args[1]
        assert update["provider_context"] == {"watch_expiration": 123}
        assert update["status"] == AccountStatus.active
        subscription_manager.ensure_subscription.assert_awaited_once_with(account)

    @pytest.mark.asyncio
    async def test_google_refresh_token_update_discards_old_history_cursor(self) -> None:
        controller, account_repo, token_service, subscription_manager = _make_controller()
        account = Account(
            uuid="grant-id",
            email="user@example.com",
            provider=AccountProvider.google,
            provider_context={"history_id": "old-history", "watch_expiration": 123},
        )
        token_service.validate_refresh_token.return_value = {"access_token": "access-token"}
        controller._fetch_account_email = AsyncMock(return_value="user@example.com")  # type: ignore[method-assign]

        with patch(
            "app.controllers.grant.custom_auth_controller.PasswordUtils.encrypt_password", return_value="encrypted"
        ):
            await controller.update_grant_refresh_token(App(id=7), account, "refresh-token")

        update = account_repo.update.await_args.args[1]
        assert update["provider_context"] == {"watch_expiration": 123}
        subscription_manager.ensure_subscription.assert_awaited_once_with(account)

    @pytest.mark.asyncio
    async def test_microsoft_refresh_token_update_preserves_provider_context(self) -> None:
        controller, account_repo, token_service, _ = _make_controller()
        account = Account(
            uuid="grant-id",
            email="user@example.com",
            provider=AccountProvider.microsoft,
            provider_context={"subscription_id": "sub-id", "history_id": "unrelated-value"},
        )
        token_service.validate_refresh_token.return_value = {"access_token": "access-token"}
        controller._fetch_account_email = AsyncMock(return_value="user@example.com")  # type: ignore[method-assign]

        with patch(
            "app.controllers.grant.custom_auth_controller.PasswordUtils.encrypt_password", return_value="encrypted"
        ):
            await controller.update_grant_refresh_token(App(id=7), account, "refresh-token")

        update = account_repo.update.await_args.args[1]
        assert update["provider_context"] == {
            "subscription_id": "sub-id",
            "history_id": "unrelated-value",
        }
