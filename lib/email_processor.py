import asyncio
import email
import logging

import aiohttp

from database import DatabaseManager
from models import AccountConfig, Config, EmailMessage

logger = logging.getLogger(__name__)


class EmailProcessor:
    """Processes new emails and sends webhooks with retry logic."""

    def __init__(self, db_manager: DatabaseManager):
        self.db_manager = db_manager
        self.http_session: aiohttp.ClientSession | None = None
        self._session_lock = asyncio.Lock()

    async def init_session(self) -> None:
        """Initialize HTTP session for webhook delivery."""
        if self.http_session is None:
            async with self._session_lock:
                if self.http_session is None:
                    timeout = aiohttp.ClientTimeout(total=Config.WEBHOOK_TIMEOUT)
                    self.http_session = aiohttp.ClientSession(
                        timeout=timeout, headers={"Content-Type": "application/json"}
                    )

    async def close_session(self) -> None:
        """Close HTTP session."""
        if self.http_session:
            await self.http_session.close()
            self.http_session = None

    async def process_email(self, account: AccountConfig, folder: str, uid: int, raw_message: bytes) -> None:
        """Process a new email and send webhook."""
        try:
            # Parse email message
            msg = email.message_from_bytes(raw_message)

            # Create email message object
            email_msg = EmailMessage(
                uid=uid,
                account=account.email,
                folder=folder,
                from_address=msg.get("From"),
                subject=msg.get("Subject"),
                raw_message=raw_message,
            )

            # Send webhook with retry logic
            # await self.send_webhook_with_retry(account, email_msg)
            print(email_msg)

            logger.info(f"Processed email UID {uid} for {account.email}:{folder}")

        except Exception as e:
            logger.error(f"Failed to process email UID {uid} for {account.email}:{folder}: {e}")
            raise

    async def send_webhook_with_retry(self, account: AccountConfig, email_msg: EmailMessage) -> bool:
        """Send webhook with exponential backoff retry logic."""
        await self.init_session()

        if not self.http_session:
            logger.error("HTTP session not initialized")
            return False

        payload = {
            "from": email_msg.from_address,
            "subject": email_msg.subject,
            "account": email_msg.account,
            "folder": email_msg.folder,
            "uid": email_msg.uid,
            "timestamp": asyncio.get_event_loop().time(),
        }

        max_retries = Config.WEBHOOK_MAX_RETRIES
        base_delay = 1.0

        for attempt in range(1, max_retries + 1):
            try:
                async with self.http_session.post(
                    account.webhook_url, json=payload, timeout=aiohttp.ClientTimeout(total=Config.WEBHOOK_TIMEOUT)
                ) as response:
                    # Log the attempt
                    await self.db_manager.log_webhook_delivery(
                        email_msg.account,
                        email_msg.folder,
                        email_msg.uid,
                        account.webhook_url,
                        response.status,
                        await response.text() if response.status != 200 else None,
                        attempt,
                        response.status == 200,
                    )

                    if response.status == 200:
                        logger.info(
                            f"Webhook delivered successfully for {email_msg.account}:{email_msg.folder} UID {email_msg.uid}"
                        )
                        return True
                    else:
                        logger.warning(
                            f"Webhook failed with status {response.status} for {email_msg.account}:{email_msg.folder} UID {email_msg.uid}"
                        )

                        # Don't retry for client errors (4xx)
                        if 400 <= response.status < 500:
                            return False

            except asyncio.TimeoutError:
                logger.warning(
                    f"Webhook timeout (attempt {attempt}) for {email_msg.account}:{email_msg.folder} UID {email_msg.uid}"
                )
                await self.db_manager.log_webhook_delivery(
                    email_msg.account,
                    email_msg.folder,
                    email_msg.uid,
                    account.webhook_url,
                    None,
                    "Timeout",
                    attempt,
                    False,
                )

            except Exception as e:
                logger.warning(
                    f"Webhook error (attempt {attempt}) for {email_msg.account}:{email_msg.folder} UID {email_msg.uid}: {e}"
                )
                await self.db_manager.log_webhook_delivery(
                    email_msg.account,
                    email_msg.folder,
                    email_msg.uid,
                    account.webhook_url,
                    None,
                    str(e),
                    attempt,
                    False,
                )

            # Exponential backoff before retry
            if attempt < max_retries:
                delay = base_delay * (2 ** (attempt - 1))
                await asyncio.sleep(delay)

        logger.error(
            f"Webhook delivery failed after {max_retries} attempts for {email_msg.account}:{email_msg.folder} UID {email_msg.uid}"
        )
        return False

    async def send_test_webhook(self, account: AccountConfig) -> bool:
        """Send a test webhook to verify connectivity."""
        await self.init_session()

        if not self.http_session:
            return False

        test_payload = {
            "test": True,
            "account": account.email,
            "message": "IMAP tracker test webhook",
            "timestamp": asyncio.get_event_loop().time(),
        }

        try:
            async with self.http_session.post(
                account.webhook_url, json=test_payload, timeout=aiohttp.ClientTimeout(total=Config.WEBHOOK_TIMEOUT)
            ) as response:
                if response.status == 200:
                    logger.info(f"Test webhook successful for {account.email}")
                    return True
                else:
                    logger.warning(f"Test webhook failed with status {response.status} for {account.email}")
                    return False

        except Exception as e:
            logger.error(f"Test webhook error for {account.email}: {e}")
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
            logger.error(f"Failed to extract email headers: {e}")
            return {}

    async def process_batch_emails(self, emails: list[tuple[AccountConfig, str, int, bytes]]) -> int:
        """Process multiple emails concurrently."""
        tasks: list[asyncio.Task[None]] = []

        for account, folder, uid, raw_message in emails:
            task = asyncio.create_task(self.process_email(account, folder, uid, raw_message))
            tasks.append(task)

        # Process emails concurrently with limited concurrency
        semaphore = asyncio.Semaphore(10)  # Limit to 10 concurrent webhook deliveries

        async def bounded_process(task: asyncio.Task[None]) -> None:
            async with semaphore:
                await task

        bounded_tasks = [bounded_process(task) for task in tasks]
        results = await asyncio.gather(*bounded_tasks, return_exceptions=True)

        # Count successful processes
        successful = sum(1 for result in results if not isinstance(result, Exception))

        logger.info(f"Processed {successful}/{len(emails)} emails successfully")
        return successful
