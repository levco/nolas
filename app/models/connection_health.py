from datetime import datetime

import sqlalchemy as sa
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base


class ConnectionHealth(Base):
    """Model for tracking connection health per account/folder combination."""

    __tablename__ = "connection_health"

    account_id: Mapped[int] = mapped_column(sa.ForeignKey("accounts.id"), nullable=False, index=True)
    folder: Mapped[str] = mapped_column(sa.String(255))
    last_success_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
    )
    consecutive_failures: Mapped[int] = mapped_column(sa.Integer, default=0, nullable=False)
    last_error: Mapped[str | None] = mapped_column(sa.Text, nullable=True)
    is_active: Mapped[bool] = mapped_column(sa.Boolean, default=True, nullable=False)

    __table_args__ = (sa.UniqueConstraint("account_id", "folder"),)

    def __repr__(self) -> str:
        return f"<ConnectionHealth(account='{self.account_id}', folder='{self.folder}', failures={self.consecutive_failures})>"
