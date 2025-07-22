import asyncio
import email
import hashlib
import hmac
import json
import logging
import uuid
from datetime import UTC, datetime
from email.message import Message as PythonEmailMessage
from uuid import UUID

import aiohttp

from app.api.payloads.messages import Message
from app.models import Account, WebhookLog
from app.repos.email import EmailRepo
from app.repos.webhook_log import WebhookLogRepo
from app.utils.message_utils import MessageUtils
from settings import settings


class EmailProcessor:
    """Processes new emails and sends webhooks with retry logic."""

    def __init__(self, webhook_log_repo: WebhookLogRepo, email_repo: EmailRepo) -> None:
        self._logger = logging.getLogger(__name__)
        self._http_session: aiohttp.ClientSession | None = None
        self._session_lock = asyncio.Lock()
        self._webhook_log_repo = webhook_log_repo
        self._email_repo = email_repo

    async def init_session(self) -> None:
        """Initialize HTTP session for webhook delivery."""
        async with self._session_lock:
            if self._http_session is None:
                timeout = aiohttp.ClientTimeout(total=settings.webhook.timeout)
                self._http_session = aiohttp.ClientSession(timeout=timeout)

    async def close_session(self) -> None:
        """Close HTTP session."""
        if self._http_session:
            await self._http_session.close()
            self._http_session = None

    def _generate_signature(self, message_body: str, webhook_secret: str) -> str:
        """
        Generate HMAC-SHA256 signature for webhook authenticity.
        This matches the Nylas webhook signature format.
        """
        if not webhook_secret:
            return ""

        try:
            digest = hmac.new(
                webhook_secret.encode("utf-8"), msg=message_body.encode("utf-8"), digestmod=hashlib.sha256
            ).hexdigest()
            return digest
        except Exception as e:
            self._logger.error(f"Error generating webhook signature: {e}")
            return ""

    async def process_email(self, account: Account, folder: str, uid: int, raw_message: PythonEmailMessage) -> Message:
        """Process a new email and send webhook."""

        nylas_message = MessageUtils.convert_to_nylas_format(msg=raw_message, grant_id=account.uuid, folder=folder)
        if nylas_message.id and await self._email_repo.get_by_account_and_email_id(account.id, nylas_message.id):
            self._logger.info(
                f"Message already exists in cache. It was likely sent via our API; account: {account.email}, "
                f"email_id: {nylas_message.id}"
            )
            return nylas_message

        await self.send_webhook_with_retry(account, folder, uid, nylas_message)
        self._logger.info(f"Processed email UID {uid} for {account.email}:{folder}")
        return nylas_message

    async def send_webhook_with_retry(self, account: Account, folder: str, uid: int, message: Message) -> bool:
        """Send webhook with exponential backoff retry logic."""
        await self.init_session()

        if not self._http_session:
            self._logger.error("HTTP session not initialized")
            return False

        webhook_uuid = uuid.uuid4()
        payload = {
            "specversion": "1.0",
            "type": "message.created",
            "source": "imap",
            "id": str(webhook_uuid),
            "time": int(asyncio.get_event_loop().time()),
            "webhook_delivery_attempt": 1,
            "data": {"application_id": str(account.app.uuid), "object": message.model_dump(by_alias=True)},
        }

        max_retries = settings.webhook.max_retries
        base_delay = 1.0

        for attempt in range(1, max_retries + 1):
            payload["webhook_delivery_attempt"] = attempt
            payload_json = json.dumps(payload)
            signature = self._generate_signature(payload_json, account.app.webhook_secret or "")
            headers = {"Content-Type": "application/json"}
            if signature:
                headers["x-nylas-signature"] = signature

            try:
                async with self._http_session.post(
                    account.app.webhook_url,
                    data=payload_json,
                    headers=headers,
                    timeout=aiohttp.ClientTimeout(total=settings.webhook.timeout),
                ) as response:
                    # Log the attempt
                    await self._log_webhook_delivery(
                        account=account,
                        webhook_uuid=webhook_uuid,
                        folder=folder,
                        uid=uid,
                        status_code=response.status,
                        response_body=await response.text() if response.status != 200 else None,
                        attempts=attempt,
                        delivered=response.status == 200,
                    )

                    if response.status == 200:
                        self._logger.info(f"Webhook delivered successfully for {account.email}:{folder} UID {uid}")
                        return True
                    else:
                        self._logger.warning(
                            f"Webhook failed with status {response.status} for {account.email}:{folder}, UID {uid}"
                        )

                        # Don't retry for client errors (4xx)
                        if 400 <= response.status < 500:
                            return False

            except asyncio.TimeoutError:
                self._logger.warning(f"Webhook timeout (attempt {attempt}) for {account.email}:{folder} UID {uid}")
                await self._log_webhook_delivery(
                    account=account,
                    webhook_uuid=webhook_uuid,
                    folder=folder,
                    uid=uid,
                    status_code=None,
                    response_body="Timeout",
                    attempts=attempt,
                    delivered=False,
                )

            except Exception as e:
                self._logger.warning(f"Webhook error (attempt {attempt}) for {account.email}:{folder} UID {uid}: {e}")
                await self._log_webhook_delivery(
                    account=account,
                    webhook_uuid=webhook_uuid,
                    folder=folder,
                    uid=uid,
                    status_code=None,
                    response_body=str(e),
                    attempts=attempt,
                    delivered=False,
                )

            # Exponential backoff before retry
            if attempt < max_retries:
                delay = base_delay * (2 ** (attempt - 1))
                await asyncio.sleep(delay)

        self._logger.error(
            f"Webhook delivery failed after {max_retries} attempts for {account.email}:{folder}, UID {uid}"
        )
        return False

    async def _log_webhook_delivery(
        self,
        account: Account,
        webhook_uuid: UUID,
        folder: str,
        uid: int,
        status_code: int | None = None,
        response_body: str | None = None,
        attempts: int = 1,
        delivered: bool = False,
    ) -> None:
        """Log webhook delivery attempt using repository."""
        try:
            await self._webhook_log_repo.persist(
                WebhookLog(
                    uuid=webhook_uuid,
                    app_id=account.app_id,
                    account_id=account.id,
                    folder=folder,
                    uid=uid,
                    webhook_url=account.app.webhook_url,
                    status_code=status_code,
                    response_body=response_body,
                    attempts=attempts,
                    delivered_at=datetime.now(UTC) if delivered else None,
                )
            )
        except Exception as e:
            self._logger.error(f"Failed to log webhook delivery: {e}")

    async def send_test_webhook(self, account: Account) -> bool:
        """Send a test webhook to verify connectivity."""
        await self.init_session()

        if not self._http_session:
            return False

        test_payload = {
            "specversion": "1.0",
            "type": "message.test",
            "source": f"/{account.email.split('@')[1]}/emails/test",
            "id": str(uuid.uuid4()),
            "time": int(asyncio.get_event_loop().time()),
            "webhook_delivery_attempt": 1,
            "data": {
                "application_id": str(account.app_id),
                "test": True,
                "account": account.email,
                "message": "IMAP tracker test webhook",
            },
        }

        # Convert payload to JSON string for signature generation
        payload_json = json.dumps(test_payload, separators=(",", ":"))

        # Generate signature for webhook authenticity
        signature = self._generate_signature(payload_json, account.app.webhook_secret or "")

        # Prepare headers
        headers = {"Content-Type": "application/json"}
        if signature:
            headers["x-nylas-signature"] = signature

        try:
            async with self._http_session.post(
                account.app.webhook_url,
                data=payload_json,
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=settings.webhook.timeout),
            ) as response:
                if response.status == 200:
                    self._logger.info(f"Test webhook successful for {account.email}")
                    return True
                else:
                    self._logger.warning(f"Test webhook failed with status {response.status} for {account.email}")
                    return False

        except Exception as e:
            self._logger.error(f"Test webhook error for {account.email}: {e}")
            return False

    async def get_email_headers(self, raw_message: bytes) -> dict[str, str]:
        """Extract email headers for webhook payload."""
        try:
            msg = email.message_from_bytes(raw_message)

            headers = {
                "from": msg.get("From"),
                "to": msg.get("To"),
                "cc": msg.get("Cc"),
                "subject": msg.get("Subject"),
                "date": msg.get("Date"),
                "message_id": msg.get("Message-ID"),
                "references": msg.get("References"),
                "in_reply_to": msg.get("In-Reply-To"),
            }

            # Remove None values
            return {k: v for k, v in headers.items() if v is not None}

        except Exception as e:
            self._logger.error(f"Failed to extract email headers: {e}")
            return {}

    async def process_batch_emails(self, emails: list[tuple[Account, str, int, PythonEmailMessage]]) -> int:
        """Process multiple emails concurrently."""
        tasks: list[asyncio.Task[Message]] = []

        for account, folder, uid, raw_message in emails:
            task = asyncio.create_task(self.process_email(account, folder, uid, raw_message))
            tasks.append(task)

        # Process emails concurrently with limited concurrency
        semaphore = asyncio.Semaphore(10)  # Limit to 10 concurrent webhook deliveries

        async def bounded_process(task: asyncio.Task[Message]) -> None:
            async with semaphore:
                await task

        bounded_tasks = [bounded_process(task) for task in tasks]
        results = await asyncio.gather(*bounded_tasks, return_exceptions=True)

        # Count successful processes
        successful = sum(1 for result in results if not isinstance(result, Exception))

        self._logger.info(f"Processed {successful}/{len(emails)} emails successfully")
        return successful
