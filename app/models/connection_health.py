from datetime import datetime

from sqlalchemy import Boolean, DateTime, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base


class ConnectionHealth(Base):
    """Model for tracking connection health per account/folder combination."""

    __tablename__ = "connection_health"

    account_email: Mapped[str] = mapped_column(String(255), primary_key=True)
    folder: Mapped[str] = mapped_column(String(255), primary_key=True)
    last_success_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    consecutive_failures: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    def __repr__(self) -> str:
        return f"<ConnectionHealth(account='{self.account_email}', folder='{self.folder}', failures={self.consecutive_failures})>"
