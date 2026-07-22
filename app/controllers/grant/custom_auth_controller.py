import logging

import aiohttp

from app.controllers.notifications.subscription_manager import SubscriptionManager
from app.controllers.providers.exceptions import ProviderAuthError, ProviderError
from app.controllers.providers.token_service import TokenService
from app.models.account import Account, AccountProvider, AccountStatus
from app.models.app import App
from app.repos.account import AccountRepo
from app.utils.password import PasswordUtils

logger = logging.getLogger(__name__)

GMAIL_PROFILE_URL = "https://gmail.googleapis.com/gmail/v1/users/me/profile"
GRAPH_ME_URL = "https://graph.microsoft.com/v1.0/me?$select=mail,userPrincipalName"


class CustomAuthController:
    """Implements POST /v3/connect/custom: create a grant from a provider refresh token.

    Mirrors Nylas custom auth: the caller (lev-backend) runs the OAuth flow with its
    own Google/Microsoft app and hands us the refresh token.
    """

    def __init__(
        self,
        account_repo: AccountRepo,
        token_service: TokenService,
        subscription_manager: SubscriptionManager,
    ) -> None:
        self._account_repo = account_repo
        self._token_service = token_service
        self._subscription_manager = subscription_manager

    async def create_grant_from_refresh_token(self, app: App, provider: AccountProvider, refresh_token: str) -> Account:
        token_payload = await self._token_service.validate_refresh_token(provider, refresh_token, app)
        access_token = token_payload["access_token"]
        # Microsoft rotates refresh tokens on every exchange.
        effective_refresh_token = token_payload.get("refresh_token") or refresh_token

        email = await self._fetch_account_email(provider, access_token)
        if not email:
            raise ProviderAuthError("Could not resolve the mailbox email address for this refresh token.")
        email = email.lower()

        account = await self._account_repo.get_by_app_and_email(app.id, email)
        if account is not None:
            # Re-auth of an existing grant: keep the UUID (grant id) stable.
            context = {
                key: value
                for key, value in (account.provider_context or {}).items()
                if key not in ("access_token", "access_token_expires_at")
                and not (provider == AccountProvider.google and key == "history_id")
            }
            await self._account_repo.update(
                account,
                {
                    "provider": provider,
                    "credentials": PasswordUtils.encrypt_password(effective_refresh_token),
                    "status": AccountStatus.active,
                    "provider_context": context,
                },
                do_commit=False,
            )
        else:
            account = Account(
                app_id=app.id,
                email=email,
                provider=provider,
                credentials=PasswordUtils.encrypt_password(effective_refresh_token),
                status=AccountStatus.active,
                provider_context={},
            )
            await self._account_repo.add(account)
        account.app = app

        # Best-effort: the renewal worker heals missing watches/subscriptions.
        try:
            await self._subscription_manager.ensure_subscription(account)
        except Exception:
            logger.exception(f"Failed to set up notifications for new grant {account.email}")

        return account

    async def update_grant_refresh_token(
        self, account: Account, refresh_token: str, app: App | None = None
    ) -> Account:
        token_payload = await self._token_service.validate_refresh_token(account.provider, refresh_token, app)
        effective_refresh_token = token_payload.get("refresh_token") or refresh_token

        # The token must belong to this grant's mailbox; otherwise a token for a
        # different account could be bound to an existing grant.
        token_email = await self._fetch_account_email(account.provider, token_payload["access_token"])
        if token_email is None or token_email.lower() != account.email.lower():
            raise ProviderAuthError(f"Refresh token belongs to a different mailbox than grant {account.uuid}.")
        context = {
            key: value
            for key, value in (account.provider_context or {}).items()
            if key not in ("access_token", "access_token_expires_at")
            and not (account.provider == AccountProvider.google and key == "history_id")
        }
        await self._account_repo.update(
            account,
            {
                "credentials": PasswordUtils.encrypt_password(effective_refresh_token),
                "status": AccountStatus.active,
                "provider_context": context,
            },
            do_commit=False,
        )
        try:
            await self._subscription_manager.ensure_subscription(account)
        except Exception:
            logger.exception(f"Failed to refresh notifications for grant {account.email}")
        return account

    async def _fetch_account_email(self, provider: AccountProvider, access_token: str) -> str | None:
        headers = {"Authorization": f"Bearer {access_token}"}
        async with aiohttp.ClientSession() as session:
            if provider == AccountProvider.google:
                async with session.get(GMAIL_PROFILE_URL, headers=headers) as response:
                    if response.status != 200:
                        raise ProviderError(f"Gmail profile lookup failed ({response.status})")
                    payload = await response.json()
                    return str(payload.get("emailAddress")) if payload.get("emailAddress") else None
            if provider == AccountProvider.microsoft:
                async with session.get(GRAPH_ME_URL, headers=headers) as response:
                    if response.status != 200:
                        raise ProviderError(f"Graph profile lookup failed ({response.status})")
                    payload = await response.json()
                    return str(payload.get("mail") or payload.get("userPrincipalName") or "") or None
        return None
