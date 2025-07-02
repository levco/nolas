from datetime import datetime

from app.models import WebhookLog
from app.repos.base import BaseRepo


class WebhookLogRepo(BaseRepo[WebhookLog]):
    """Repository for WebhookLog model operations."""

    def __init__(self) -> None:
        super().__init__(WebhookLog)

    async def log_delivery(
        self,
        account_email: str,
        folder: str,
        uid: int,
        status_code: int | None,
        response_body: str | None,
        error_message: str | None,
        attempts: int = 1,
    ) -> WebhookLog:
        """Log a webhook delivery attempt."""
        webhook_log = WebhookLog(
            account_email=account_email,
            folder=folder,
            uid=uid,
            status_code=status_code,
            response_body=response_body,
            error_message=error_message,
            attempts=attempts,
        )
        self._db.session.add(webhook_log)
        await self._db.session.flush()
        return webhook_log

    async def create_log(
        self,
        account_email: str,
        folder: str,
        uid: int,
        webhook_url: str,
        status_code: int | None = None,
        response_body: str | None = None,
        attempts: int = 1,
        delivered_at: datetime | None = None,
    ) -> WebhookLog:
        """Create a new webhook log entry."""
        webhook_log = WebhookLog(
            account_email=account_email,
            folder=folder,
            uid=uid,
            webhook_url=webhook_url,
            status_code=status_code,
            response_body=response_body,
            attempts=attempts,
            delivered_at=delivered_at,
        )

        self._db.session.add(webhook_log)
        await self._db.session.flush()
        await self._db.session.refresh(webhook_log)
        return webhook_log
