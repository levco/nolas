import logging
from typing import Any

from app.controllers.notifications.bounce import detect_bounce
from app.controllers.providers.exceptions import (
    ProviderAuthError,
    ProviderError,
    ProviderNotFoundError,
)
from app.controllers.providers.google.gmail_client import GmailClient
from app.controllers.providers.microsoft.graph_client import GraphClient
from app.models.account import Account, AccountProvider, AccountStatus
from app.models.job import JobType
from app.repos.account import AccountRepo
from app.repos.email import EmailRepo
from app.repos.job import JobRepo

logger = logging.getLogger(__name__)

GMAIL_SENT_FOLDERS = {"SENT"}
MAX_HISTORY_PAGES = 20


class IncomingNotificationController:
    """Turns provider notifications into Nylas-shaped message webhooks."""

    def __init__(
        self,
        account_repo: AccountRepo,
        email_repo: EmailRepo,
        job_repo: JobRepo,
        gmail_client: GmailClient,
        graph_client: GraphClient,
    ) -> None:
        self._account_repo = account_repo
        self._email_repo = email_repo
        self._job_repo = job_repo
        self._gmail = gmail_client
        self._graph = graph_client

    # --- Google ---

    async def process_google_notification(self, email_address: str, history_id: str) -> None:
        try:
            accounts = await self._account_repo.get_all_by_email_and_provider(email_address, AccountProvider.google)
        except Exception:
            logger.exception(
                f"Failed loading Google accounts for notification (email={email_address}, history_id={history_id})"
            )
            raise
        logger.info(f"Google notification for {email_address}: {len(accounts)} matching account(s)")
        for account in accounts:
            try:
                await self._account_repo.acquire_notification_lock(account.id)
                await self._account_repo.refresh_from_db(account)
                if account.status != AccountStatus.active:
                    logger.info(f"Account {account.email} is not active; skipping")
                    continue
                await self.catch_up_google_history(account, history_id)
                logger.info(f"Processed account: {account.email}")
            except ProviderAuthError:
                logger.warning(f"Auth failure processing Gmail notification for {account.email}")
            except Exception:
                logger.exception(f"Failed to process Gmail notification for {account.email}")

    async def catch_up_google_history(self, account: Account, notified_history_id: str) -> None:
        """Fetch and process any Gmail history between the stored cursor and `notified_history_id`.

        Used both for live Pub/Sub notifications and for watch-renewal, where the new watch's
        historyId may be ahead of our cursor if a push notification was missed during the gap.
        """
        context: dict[str, Any] = {**(account.provider_context or {})}
        start_history_id = context.get("history_id")
        if not start_history_id:
            # First notification primes the cursor.
            context["history_id"] = str(notified_history_id)
            await self._account_repo.update(account, {"provider_context": context}, do_commit=False)
            return

        created_message_ids: dict[str, None] = {}
        updated_message_ids: dict[str, None] = {}
        page_token: str | None = None
        latest_history_id = str(notified_history_id)
        last_entry_id: str | None = None
        exhausted = False
        try:
            for _ in range(MAX_HISTORY_PAGES):
                history = await self._gmail.list_history(account, str(start_history_id), page_token)
                latest_history_id = str(history.get("historyId", latest_history_id))
                for entry in history.get("history", []):
                    if entry.get("id"):
                        last_entry_id = str(entry["id"])
                    for added in entry.get("messagesAdded", []):
                        message = added.get("message", {})
                        labels = message.get("labelIds", [])
                        if "DRAFT" in labels:
                            continue
                        if message.get("id"):
                            created_message_ids[message["id"]] = None
                    for field in ("labelsAdded", "labelsRemoved"):
                        for changed in entry.get(field, []):
                            message = changed.get("message", {})
                            labels = message.get("labelIds", [])
                            if "DRAFT" in labels:
                                continue
                            if message.get("id"):
                                updated_message_ids[message["id"]] = None
                page_token = history.get("nextPageToken")
                if not page_token:
                    exhausted = True
                    break
        except ProviderNotFoundError:
            # startHistoryId too old — re-prime from the current profile.
            profile = await self._gmail.get_profile(account)
            context["history_id"] = str(profile.get("historyId", notified_history_id))
            await self._account_repo.update(account, {"provider_context": context}, do_commit=False)
            logger.warning(f"Gmail history expired for {account.email}; cursor reset")
            return

        # A newly-added message can also have label records in the same history
        # window. Emit one event with its latest state, with message.created taking
        # precedence over message.updated.
        for message_id in created_message_ids:
            updated_message_ids.pop(message_id, None)

        if created_message_ids or updated_message_ids:
            logger.info(
                f"Processing Gmail changes; email={account.email}, "
                f"created={list(created_message_ids)}, updated={list(updated_message_ids)}"
            )
        for message_id in created_message_ids:
            await self._emit_message_event(
                account, message_id, provider=AccountProvider.google, event_type="message.created"
            )
        for message_id in updated_message_ids:
            await self._emit_message_event(
                account, message_id, provider=AccountProvider.google, event_type="message.updated"
            )

        # If the page cap stopped us early, only advance the cursor to the last entry we
        # actually processed — advancing to the mailbox-latest id would drop the tail.
        if exhausted:
            context["history_id"] = latest_history_id
        elif last_entry_id is not None:
            context["history_id"] = last_entry_id
            logger.warning(
                f"Gmail history page cap reached for {account.email}; cursor advanced to {last_entry_id} only"
            )
        await self._account_repo.update(account, {"provider_context": context}, do_commit=False)

    # --- Microsoft ---

    async def process_microsoft_notification(self, notification: dict[str, Any]) -> None:
        subscription_id = notification.get("subscriptionId")
        client_state = notification.get("clientState")
        resource_data = notification.get("resourceData") or {}
        message_id = resource_data.get("id")
        if not subscription_id or not message_id:
            return

        account = await self._account_repo.get_by_subscription_id(subscription_id)
        if account is None:
            logger.warning(f"No account found for Graph subscription {subscription_id}")
            return
        await self._account_repo.acquire_notification_lock(account.id)
        await self._account_repo.refresh_from_db(account)
        if account.status != AccountStatus.active:
            return
        expected_state = (account.provider_context or {}).get("client_state")
        if expected_state and client_state != expected_state:
            logger.warning(f"clientState mismatch for Graph subscription {subscription_id}; dropping")
            return

        change_type = notification.get("changeType")
        event_type = {
            "created": "message.created",
            "updated": "message.updated",
        }.get(change_type if isinstance(change_type, str) else "")
        if event_type is None:
            logger.debug(f"Ignoring unsupported Graph message change type: {change_type}")
            return

        try:
            await self._emit_message_event(
                account, message_id, provider=AccountProvider.microsoft, event_type=event_type
            )
        except ProviderAuthError:
            logger.warning(f"Auth failure processing Graph notification for {account.email}")
        except Exception:
            logger.exception(f"Failed to process Graph notification for {account.email}")

    # --- Shared ---

    async def _emit_message_event(
        self,
        account: Account,
        message_id: str,
        provider: AccountProvider,
        event_type: str,
    ) -> None:
        client = self._gmail if provider == AccountProvider.google else self._graph
        try:
            message = await client.get_message(account, message_id, include_headers=True)
        except ProviderNotFoundError:
            return
        except ProviderError as e:
            logger.warning(f"Could not fetch message {message_id} for {account.email}: {e.message}")
            return
        if message is None:
            return
        if provider == AccountProvider.microsoft and not message.folders:
            return

        await self._job_repo.enqueue(
            JobType.webhook_delivery,
            {
                "account_id": account.id,
                "event_type": event_type,
                "source": "nolas",
                "object_data": message.model_dump(by_alias=True),
                "email_id": message.id,
                "thread_id": message.thread_id,
            },
            max_attempts=10,
        )

        # Read-state, label, and folder updates must not re-emit bounce events.
        bounce = detect_bounce(message) if event_type == "message.created" else None
        if bounce:
            try:
                await self._job_repo.enqueue(
                    JobType.webhook_delivery,
                    {
                        "account_id": account.id,
                        "event_type": "message.bounce_detected",
                        "source": "nolas",
                        "object_data": bounce,
                        "email_id": message.id,
                        "thread_id": message.thread_id,
                    },
                    max_attempts=10,
                )
            except Exception:
                # Bounce events are best-effort and should not block message.created processing.
                logger.warning(f"Failed to enqueue bounce webhook for {message.id} ({account.email})", exc_info=True)
