from datetime import datetime

from sqlalchemy import BigInteger, DateTime, String, func
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base


class UidTracking(Base):
    """Model for tracking last seen UIDs per account/folder combination."""

    __tablename__ = "uid_tracking"

    account_email: Mapped[str] = mapped_column(String(255), primary_key=True)
    folder: Mapped[str] = mapped_column(String(255), primary_key=True)
    last_seen_uid: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    last_checked_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    def __repr__(self) -> str:
        return f"<UidTracking(account='{self.account_email}', folder='{self.folder}', last_uid={self.last_seen_uid})>"
