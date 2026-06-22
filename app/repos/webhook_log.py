from datetime import UTC, datetime, timedelta

from fastapi_async_sqlalchemy import db
from sqlalchemy import delete

from app.models import WebhookLog
from app.repos.base import BaseRepo


class WebhookLogRepo(BaseRepo[WebhookLog]):
    """Repository for WebhookLog model operations."""

    def __init__(self) -> None:
        super().__init__(WebhookLog)

    async def delete_older_than(self, days: int) -> int:
        """Delete webhook logs older than the given number of days. Returns rows deleted."""
        cutoff = datetime.now(UTC) - timedelta(days=days)
        result = await db.session.execute(delete(WebhookLog).where(WebhookLog.created_at < cutoff))
        return result.rowcount or 0
