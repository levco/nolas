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
    """Turns Gmail Pub/Sub pushes and Graph change notifications into message.created webhooks."""

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
                await self._process_google_account(account, history_id)
                logger.info(f"Processed account: {account.email}")
            except ProviderAuthError:
                logger.warning(f"Auth failure processing Gmail notification for {account.email}")
            except Exception:
                logger.exception(f"Failed to process Gmail notification for {account.email}")

    async def _process_google_account(self, account: Account, notified_history_id: str) -> None:
        context: dict[str, Any] = {**(account.provider_context or {})}
        start_history_id = context.get("history_id")
        if not start_history_id:
            # First notification primes the cursor.
            context["history_id"] = str(notified_history_id)
            await self._account_repo.update(account, {"provider_context": context}, do_commit=False)
            return

        message_ids: list[str] = []
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
                            message_ids.append(message["id"])
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

        for message_id in dict.fromkeys(message_ids):
            await self._emit_message_created(account, message_id, provider=AccountProvider.google)

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

        try:
            await self._emit_message_created(account, message_id, provider=AccountProvider.microsoft)
        except ProviderAuthError:
            logger.warning(f"Auth failure processing Graph notification for {account.email}")
        except Exception:
            logger.exception(f"Failed to process Graph notification for {account.email}")

    # --- Shared ---

    async def _emit_message_created(self, account: Account, message_id: str, provider: AccountProvider) -> None:
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
                "event_type": "message.created",
                "source": "nolas",
                "object_data": message.model_dump(by_alias=True),
                "email_id": message.id,
                "thread_id": message.thread_id,
            },
            max_attempts=10,
        )

        bounce = detect_bounce(message)
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
