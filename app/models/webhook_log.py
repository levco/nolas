from datetime import datetime
from typing import TYPE_CHECKING

import sqlalchemy as sa
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .base import Base, TimestampMixin

if TYPE_CHECKING:
    from .account import Account


class WebhookLog(Base, TimestampMixin):
    """Model for logging webhook delivery attempts."""

    __tablename__ = "webhook_logs"

    app_id: Mapped[int] = mapped_column(sa.ForeignKey("apps.id"), nullable=False, index=True)
    account_id: Mapped[int] = mapped_column(sa.ForeignKey("accounts.id"), nullable=False, index=True)
    folder: Mapped[str] = mapped_column(sa.String(255), nullable=False)
    uid: Mapped[int] = mapped_column(sa.BigInteger, nullable=False)
    webhook_url: Mapped[str] = mapped_column(sa.Text, nullable=False)
    status_code: Mapped[int | None] = mapped_column(sa.Integer, nullable=True)
    response_body: Mapped[str | None] = mapped_column(sa.Text, nullable=True)
    attempts: Mapped[int] = mapped_column(sa.Integer, default=1, nullable=False)
    delivered_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True), nullable=True)

    account: Mapped["Account"] = relationship("Account")

    def __repr__(self) -> str:
        return (
            f"<WebhookLog(app='{self.app_id}', account='{self.account_id}', folder='{self.folder}', uid={self.uid}, "
            f"status={self.status_code})>"
        )
