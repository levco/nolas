from datetime import datetime

import sqlalchemy as sa
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base, TimestampMixin


class UidTracking(Base, TimestampMixin):
    """Model for tracking last seen UIDs per account/folder combination."""

    __tablename__ = "uid_tracking"

    account_id: Mapped[int] = mapped_column(sa.ForeignKey("accounts.id"), nullable=False, index=True)
    folder: Mapped[str] = mapped_column(sa.String(255))
    last_seen_uid: Mapped[int] = mapped_column(sa.BigInteger, default=0, nullable=False)
    last_checked_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
    )

    __table_args__ = (sa.UniqueConstraint("account_id", "folder"),)

    def __repr__(self) -> str:
        return f"<UidTracking(account='{self.account_id}', folder='{self.folder}', last_uid={self.last_seen_uid})>"
