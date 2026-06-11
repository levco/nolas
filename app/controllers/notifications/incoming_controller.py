import logging
from typing import Any

from sqlalchemy.exc import IntegrityError

from app.controllers.notifications.bounce import detect_bounce
from app.controllers.providers.exceptions import ProviderAuthError, ProviderError, ProviderNotFoundError
from app.controllers.providers.google.gmail_client import GmailClient
from app.controllers.providers.microsoft.graph_client import GraphClient
from app.controllers.webhooks.sender import WebhookSender
from app.models import Email
from app.models.account import Account, AccountProvider, AccountStatus
from app.repos.account import AccountRepo
from app.repos.email import EmailRepo
from settings import settings

logger = logging.getLogger(__name__)

GMAIL_SENT_FOLDERS = {"SENT"}
MAX_HISTORY_PAGES = 20


class IncomingNotificationController:
    """Turns Gmail Pub/Sub pushes and Graph change notifications into message.created webhooks."""

    def __init__(
        self,
        account_repo: AccountRepo,
        email_repo: EmailRepo,
        gmail_client: GmailClient,
        graph_client: GraphClient,
        webhook_sender: WebhookSender,
    ) -> None:
        self._account_repo = account_repo
        self._email_repo = email_repo
        self._gmail = gmail_client
        self._graph = graph_client
        self._webhook_sender = webhook_sender

    # --- Google ---

    async def process_google_notification(self, email_address: str, history_id: str) -> None:
        accounts = await self._account_repo.get_all_by_email_and_provider(email_address, AccountProvider.google)
        for account in accounts:
            if account.status != AccountStatus.active:
                continue
            try:
                await self._process_google_account(account, history_id)
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
        cached = await self._email_repo.get_by_account_and_email_id(account.id, message_id)
        if cached:
            # Already known — typically a message sent through our API.
            return

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

        email_row = Email(
            account_id=account.id,
            email_id=message.id,
            thread_id=message.thread_id,
            folder=message.folders[0] if message.folders else "",
        )
        try:
            await self._email_repo.add(email_row)
        except IntegrityError:
            # Unique (account_id, email_id) violation — another worker beat us to it.
            await self._email_repo.rollback()
            logger.info(f"Duplicate notification for message {message_id} ({account.email}); skipping")
            return

        delivered = await self._webhook_sender.send_event(account, "message.created", message.model_dump(by_alias=True))
        if not delivered:
            # Remove the dedup row so a later notification or replay can re-emit;
            # leaving it would permanently suppress this message downstream.
            await self._email_repo.delete(email_row)
            logger.warning(f"message.created not delivered for {message.id} ({account.email}); dedup row removed")
            return

        bounce = detect_bounce(message)
        if bounce:
            await self._webhook_sender.send_event(account, "message.bounce_detected", bounce)

    @property
    def google_verification_token(self) -> str:
        return settings.google.pubsub_verification_token
