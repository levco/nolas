import logging
import secrets
import time
from typing import Any

from app.controllers.providers.exceptions import ProviderAuthError, ProviderError
from app.controllers.providers.google.gmail_client import GmailClient
from app.controllers.providers.microsoft.graph_client import GraphClient
from app.models.account import Account, AccountProvider
from app.repos.account import AccountRepo
from settings import settings

logger = logging.getLogger(__name__)


class SubscriptionManager:
    """Creates and renews Gmail watches and Microsoft Graph change-notification subscriptions."""

    def __init__(self, account_repo: AccountRepo, gmail_client: GmailClient, graph_client: GraphClient) -> None:
        self._account_repo = account_repo
        self._gmail = gmail_client
        self._graph = graph_client
        self._logger = logging.getLogger(__name__)

    async def ensure_subscription(self, account: Account) -> None:
        if account.provider == AccountProvider.google:
            await self._ensure_google_watch(account)
        elif account.provider == AccountProvider.microsoft:
            await self._ensure_microsoft_subscription(account)

    async def _ensure_google_watch(self, account: Account) -> None:
        if not settings.google.pubsub_topic:
            logger.warning("GOOGLE_PUBSUB_TOPIC not configured; skipping Gmail watch setup")
            return
        response = await self._gmail.watch(account, settings.google.pubsub_topic)
        self._logger.info(f"Gmail watch response: {response}")
        context: dict[str, Any] = {**(account.provider_context or {})}
        # expiration is epoch millis as string.
        context["watch_expiration"] = int(int(response.get("expiration", 0)) / 1000)
        # Prime the history cursor on first setup only — history.list resumes from here.
        if not context.get("history_id") and response.get("historyId"):
            context["history_id"] = str(response["historyId"])
        await self._account_repo.update(account, {"provider_context": context}, do_commit=False)
        logger.info(f"Gmail watch ensured for {account.email} (expires {context['watch_expiration']})")

    async def _ensure_microsoft_subscription(self, account: Account) -> None:
        if not settings.api.public_base_url:
            logger.warning("API_PUBLIC_BASE_URL not configured; skipping Graph subscription setup")
            return
        context: dict[str, Any] = {**(account.provider_context or {})}
        notification_url = f"{settings.api.public_base_url.rstrip('/')}/v3/notifications/microsoft"

        subscription_id = context.get("subscription_id")
        if subscription_id:
            try:
                response = await self._graph.renew_subscription(account, subscription_id)
                context["subscription_expires_at"] = _iso_to_epoch(response.get("expirationDateTime"))
                await self._account_repo.update(account, {"provider_context": context}, do_commit=False)
                logger.info(f"Graph subscription renewed for {account.email}")
                return
            except ProviderAuthError:
                raise
            except ProviderError:
                logger.warning(f"Graph subscription renewal failed for {account.email}; recreating")

        client_state = context.get("client_state") or secrets.token_urlsafe(24)
        response = await self._graph.create_subscription(account, notification_url, client_state)
        context.update(
            {
                "subscription_id": response.get("id"),
                "subscription_expires_at": _iso_to_epoch(response.get("expirationDateTime")),
                "client_state": client_state,
            }
        )
        await self._account_repo.update(account, {"provider_context": context}, do_commit=False)
        logger.info(f"Graph subscription created for {account.email}")

    async def teardown(self, account: Account) -> None:
        try:
            if account.provider == AccountProvider.google:
                await self._gmail.stop_watch(account)
            elif account.provider == AccountProvider.microsoft:
                subscription_id = (account.provider_context or {}).get("subscription_id")
                if subscription_id:
                    await self._graph.delete_subscription(account, subscription_id)
        except Exception:
            logger.warning(f"Failed to tear down notifications for {account.email}", exc_info=True)

    def needs_renewal(self, account: Account, within_seconds: int) -> bool:
        context = account.provider_context or {}
        deadline = time.time() + within_seconds
        if account.provider == AccountProvider.google:
            return bool(int(context.get("watch_expiration", 0)) < deadline)
        if account.provider == AccountProvider.microsoft:
            return bool(int(context.get("subscription_expires_at", 0)) < deadline)
        return False


def _iso_to_epoch(iso_timestamp: str | None) -> int:
    if not iso_timestamp:
        return 0
    from datetime import datetime

    try:
        return int(datetime.fromisoformat(iso_timestamp.replace("Z", "+00:00")).timestamp())
    except ValueError:
        return 0
