import asyncio
import logging
import time
from typing import Any

import aiohttp

from app.controllers.providers.exceptions import ProviderAuthError, ProviderError
from app.controllers.webhooks.sender import WebhookSender
from app.models.account import Account, AccountProvider, AccountStatus
from app.repos.account import AccountRepo
from app.utils.password import PasswordUtils
from settings import settings

logger = logging.getLogger(__name__)

GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"

# Refresh access tokens this many seconds before they expire.
EXPIRY_MARGIN_SECONDS = 120

# Nylas grant.expired provider codes consumed by downstream listeners.
GRANT_EXPIRED_CODE = 25009


class TokenService:
    """Issues provider access tokens from stored refresh tokens.

    Access tokens are cached (encrypted) in account.provider_context. A permanently
    invalid refresh token marks the account expired and emits a grant.expired webhook.
    """

    def __init__(self, account_repo: AccountRepo, webhook_sender: WebhookSender) -> None:
        self._account_repo = account_repo
        self._webhook_sender = webhook_sender
        self._http_session: aiohttp.ClientSession | None = None
        self._session_lock = asyncio.Lock()
        self._refresh_locks: dict[int, asyncio.Lock] = {}

    async def _get_session(self) -> aiohttp.ClientSession:
        async with self._session_lock:
            if self._http_session is None or self._http_session.closed:
                self._http_session = aiohttp.ClientSession()
            return self._http_session

    async def close(self) -> None:
        if self._http_session and not self._http_session.closed:
            await self._http_session.close()

    async def get_access_token(self, account: Account, force_refresh: bool = False) -> str:
        context = account.provider_context or {}
        encrypted_token = context.get("access_token")
        expires_at = context.get("access_token_expires_at", 0)
        if not force_refresh and encrypted_token and expires_at > time.time() + EXPIRY_MARGIN_SECONDS:
            return PasswordUtils.decrypt_password(encrypted_token)

        lock = self._refresh_locks.setdefault(account.id, asyncio.Lock())
        async with lock:
            # Microsoft rotates refresh tokens on redemption, so a concurrent refresh from
            # another replica is destructive. Serialize cross-replica via a Postgres
            # advisory lock and re-read the row in case the other replica already rotated.
            if account.provider == AccountProvider.microsoft:
                await self._account_repo.acquire_refresh_lock(account.id)
                await self._account_repo.refresh_from_db(account)

            # Re-check after acquiring the lock(s): another task may have refreshed.
            context = account.provider_context or {}
            encrypted_token = context.get("access_token")
            expires_at = context.get("access_token_expires_at", 0)
            if not force_refresh and encrypted_token and expires_at > time.time() + EXPIRY_MARGIN_SECONDS:
                return PasswordUtils.decrypt_password(encrypted_token)
            return await self._refresh_access_token(account)

    async def _refresh_access_token(self, account: Account) -> str:
        refresh_token = PasswordUtils.decrypt_password(account.credentials)
        if account.provider == AccountProvider.google:
            token_data = await self._refresh_google(refresh_token)
        elif account.provider == AccountProvider.microsoft:
            token_data = await self._refresh_microsoft(refresh_token)
        else:
            raise ProviderError(f"Token refresh not supported for provider {account.provider.value}")

        access_token: str = token_data["access_token"]
        expires_in: int = int(token_data.get("expires_in", 3600))

        update: dict[str, Any] = {
            "provider_context": {
                **(account.provider_context or {}),
                "access_token": PasswordUtils.encrypt_password(access_token),
                "access_token_expires_at": int(time.time()) + expires_in,
            }
        }
        # Microsoft rotates refresh tokens; persist the new one when returned.
        new_refresh_token = token_data.get("refresh_token")
        if new_refresh_token and new_refresh_token != refresh_token:
            update["credentials"] = PasswordUtils.encrypt_password(new_refresh_token)

        await self._account_repo.update(account, update, do_commit=False)
        return access_token

    async def _refresh_google(self, refresh_token: str) -> dict[str, Any]:
        session = await self._get_session()
        payload = {
            "client_id": settings.google.client_id,
            "client_secret": settings.google.client_secret,
            "refresh_token": refresh_token,
            "grant_type": "refresh_token",
        }
        async with session.post(GOOGLE_TOKEN_URL, data=payload) as response:
            body = await response.json()
            if response.status == 200:
                return dict(body)
            if body.get("error") in ("invalid_grant", "unauthorized_client"):
                raise ProviderAuthError(f"Google refresh token rejected: {body.get('error_description', '')}")
            raise ProviderError(f"Google token refresh failed ({response.status}): {body}")

    async def _refresh_microsoft(self, refresh_token: str) -> dict[str, Any]:
        session = await self._get_session()
        token_url = f"{settings.microsoft.authority.rstrip('/')}/oauth2/v2.0/token"
        payload = {
            "client_id": settings.microsoft.client_id,
            "client_secret": settings.microsoft.client_secret,
            "refresh_token": refresh_token,
            "grant_type": "refresh_token",
            "scope": settings.microsoft.scopes,
        }
        async with session.post(token_url, data=payload) as response:
            body = await response.json()
            if response.status == 200:
                return dict(body)
            if body.get("error") in ("invalid_grant", "interaction_required"):
                raise ProviderAuthError(f"Microsoft refresh token rejected: {body.get('error_description', '')}")
            raise ProviderError(f"Microsoft token refresh failed ({response.status}): {body}")

    async def handle_auth_failure(self, account: Account) -> None:
        """Mark the grant expired and notify the app. Idempotent."""
        if account.status == AccountStatus.expired:
            return
        logger.warning(f"Marking grant expired for account {account.email} ({account.provider.value})")
        await self._account_repo.update(account, {"status": AccountStatus.expired}, do_commit=False)
        await self._webhook_sender.send_event(
            account,
            "grant.expired",
            {
                "code": GRANT_EXPIRED_CODE,
                "grant_id": str(account.uuid),
                "integration_id": str(account.app.uuid),
                "login_id": account.email,
                "provider": account.provider.value,
            },
        )

    async def validate_refresh_token(self, provider: AccountProvider, refresh_token: str) -> dict[str, Any]:
        """Exchange a refresh token once to validate it. Returns the token payload."""
        if provider == AccountProvider.google:
            return await self._refresh_google(refresh_token)
        if provider == AccountProvider.microsoft:
            return await self._refresh_microsoft(refresh_token)
        raise ProviderError(f"Refresh-token validation not supported for provider {provider.value}")
