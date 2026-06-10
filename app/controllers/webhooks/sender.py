import asyncio
import hashlib
import hmac
import json
import logging
import time
import uuid
from datetime import UTC, datetime
from typing import Any

import aiohttp

from app.models import Account, WebhookLog
from app.repos.webhook_log import WebhookLogRepo
from settings import settings

logger = logging.getLogger(__name__)

GRANT_EVENT_PREFIX = "grant."


class WebhookSender:
    """Delivers Nylas-shaped webhook events to the owning app with retry + logging.

    Event envelope matches the Nylas v3 webhook schema consumed by downstream
    listeners: {specversion, type, source, id, time, data: {application_id, grant_id, object}}.
    """

    def __init__(self, webhook_log_repo: WebhookLogRepo) -> None:
        self._webhook_log_repo = webhook_log_repo
        self._http_session: aiohttp.ClientSession | None = None
        self._session_lock = asyncio.Lock()

    async def init_session(self) -> None:
        async with self._session_lock:
            if self._http_session is None:
                timeout = aiohttp.ClientTimeout(total=settings.webhook.timeout)
                self._http_session = aiohttp.ClientSession(timeout=timeout)

    async def close_session(self) -> None:
        if self._http_session:
            await self._http_session.close()
            self._http_session = None

    async def send_event(
        self,
        account: Account,
        event_type: str,
        object_data: dict[str, Any],
        source: str = "nolas",
    ) -> bool:
        """Send a webhook event for an account. Returns True when delivered (2xx)."""
        await self.init_session()
        assert self._http_session is not None

        app = account.app
        webhook_url = app.webhook_url
        if event_type.startswith(GRANT_EVENT_PREFIX) and app.grant_webhook_url:
            webhook_url = app.grant_webhook_url
        if not webhook_url:
            logger.warning(f"No webhook URL configured for app {app.id}; dropping {event_type}")
            return False

        webhook_uuid = uuid.uuid4()
        payload: dict[str, Any] = {
            "specversion": "1.0",
            "type": event_type,
            "source": source,
            "id": str(webhook_uuid),
            "time": int(time.time()),
            "webhook_delivery_attempt": 1,
            "data": {
                "application_id": str(app.uuid),
                "grant_id": str(account.uuid),
                "object": object_data,
            },
        }

        max_retries = settings.webhook.max_retries
        base_delay = 1.0

        for attempt in range(1, max_retries + 1):
            payload["webhook_delivery_attempt"] = attempt
            payload_json = json.dumps(payload)
            headers = {"Content-Type": "application/json"}
            signature = self._generate_signature(payload_json, app.webhook_secret or "")
            if signature:
                headers["x-nylas-signature"] = signature

            try:
                async with self._http_session.post(
                    webhook_url,
                    data=payload_json,
                    headers=headers,
                    timeout=aiohttp.ClientTimeout(total=settings.webhook.timeout),
                ) as response:
                    delivered = 200 <= response.status < 300
                    await self._log_delivery(
                        account=account,
                        webhook_uuid=webhook_uuid,
                        webhook_url=webhook_url,
                        status_code=response.status,
                        response_body=None if delivered else await response.text(),
                        attempts=attempt,
                        delivered=delivered,
                    )
                    if delivered:
                        return True
                    if 400 <= response.status < 500:
                        logger.warning(
                            f"Webhook {event_type} rejected with {response.status} for account {account.email}"
                        )
                        return False
            except asyncio.TimeoutError:
                await self._log_delivery(account, webhook_uuid, webhook_url, None, "Timeout", attempt, delivered=False)
            except Exception as e:
                await self._log_delivery(account, webhook_uuid, webhook_url, None, str(e), attempt, delivered=False)

            if attempt < max_retries:
                await asyncio.sleep(base_delay * (2 ** (attempt - 1)))

        logger.error(f"Webhook {event_type} delivery failed after {max_retries} attempts for {account.email}")
        return False

    def _generate_signature(self, message_body: str, webhook_secret: str) -> str:
        if not webhook_secret:
            return ""
        try:
            return hmac.new(
                webhook_secret.encode("utf-8"), msg=message_body.encode("utf-8"), digestmod=hashlib.sha256
            ).hexdigest()
        except Exception:
            logger.exception("Error generating webhook signature")
            return ""

    async def _log_delivery(
        self,
        account: Account,
        webhook_uuid: uuid.UUID,
        webhook_url: str,
        status_code: int | None,
        response_body: str | None,
        attempts: int,
        delivered: bool,
    ) -> None:
        try:
            await self._webhook_log_repo.persist(
                WebhookLog(
                    uuid=webhook_uuid,
                    app_id=account.app_id,
                    account_id=account.id,
                    folder=None,
                    uid=None,
                    webhook_url=webhook_url,
                    status_code=status_code,
                    response_body=response_body,
                    attempts=attempts,
                    delivered_at=datetime.now(UTC) if delivered else None,
                )
            )
        except Exception:
            logger.exception("Failed to log webhook delivery")
