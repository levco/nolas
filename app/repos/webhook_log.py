from datetime import datetime
from typing import Optional

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import WebhookLog


class WebhookLogRepo:
    """Repository for WebhookLog model operations."""

    def __init__(self, session: AsyncSession):
        self._session = session

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
        self._session.add(webhook_log)
        await self._session.flush()
        return webhook_log

    async def get_by_account_folder(self, account_email: str, folder: str) -> list[WebhookLog]:
        """Get webhook logs by account and folder."""
        result = await self._session.execute(
            select(WebhookLog)
            .where(WebhookLog.account_email == account_email, WebhookLog.folder == folder)
            .order_by(WebhookLog.created_at.desc())
        )
        return list(result.scalars().all())

    async def get_failed_deliveries(self, limit: int = 100) -> list[WebhookLog]:
        """Get failed webhook deliveries."""
        result = await self._session.execute(
            select(WebhookLog)
            .where(WebhookLog.status_code.is_(None) | (WebhookLog.status_code >= 400))
            .order_by(WebhookLog.created_at.desc())
            .limit(limit)
        )
        return list(result.scalars().all())

    async def cleanup_old_logs(self, days: int = 30) -> int:
        """Clean up old webhook logs."""
        cutoff_date = func.now() - func.interval(f"{days} days")
        result = await self._session.execute(delete(WebhookLog).where(WebhookLog.created_at < cutoff_date))
        await self._session.flush()
        return result.rowcount

    async def create_log(
        self,
        account_email: str,
        folder: str,
        uid: int,
        webhook_url: str,
        status_code: Optional[int] = None,
        response_body: Optional[str] = None,
        attempts: int = 1,
        delivered_at: Optional[datetime] = None,
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

        self._session.add(webhook_log)
        await self._session.flush()
        await self._session.refresh(webhook_log)
        return webhook_log

    async def get_by_id(self, webhook_log_id: int) -> Optional[WebhookLog]:
        """Get webhook log by ID."""
        result = await self._session.execute(select(WebhookLog).where(WebhookLog.id == webhook_log_id))
        return result.scalar_one_or_none()

    async def get_logs_for_account(self, account_email: str, limit: int = 100, offset: int = 0) -> list[WebhookLog]:
        """Get webhook logs for an account."""
        result = await self._session.execute(
            select(WebhookLog)
            .where(WebhookLog.account_email == account_email)
            .order_by(WebhookLog.created_at.desc())
            .limit(limit)
            .offset(offset)
        )
        return list(result.scalars().all())

    async def get_delivery_stats(self, account_email: str) -> dict[str, int]:
        """Get delivery statistics for an account."""
        # Total logs
        total_result = await self._session.execute(
            select(func.count(WebhookLog.id)).where(WebhookLog.account_email == account_email)
        )
        total = total_result.scalar() or 0

        # Delivered logs
        delivered_result = await self._session.execute(
            select(func.count(WebhookLog.id)).where(
                WebhookLog.account_email == account_email, WebhookLog.delivered_at.is_not(None)
            )
        )
        delivered = delivered_result.scalar() or 0

        # Failed logs (max attempts reached)
        failed_result = await self._session.execute(
            select(func.count(WebhookLog.id)).where(
                WebhookLog.account_email == account_email, WebhookLog.attempts >= 3, WebhookLog.delivered_at.is_(None)
            )
        )
        failed = failed_result.scalar() or 0

        return {"total": total, "delivered": delivered, "failed": failed, "pending": total - delivered - failed}
