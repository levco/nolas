import sqlalchemy as sa
from sqlalchemy import UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base, TimestampMixin


class Email(Base, TimestampMixin):
    """Model for tracking email messages."""

    __tablename__ = "emails"

    email_id: Mapped[str] = mapped_column(sa.String(255), nullable=False)
    thread_id: Mapped[str] = mapped_column(sa.String(255), nullable=False)
    account_id: Mapped[int] = mapped_column(sa.ForeignKey("accounts.id"), nullable=False)
    folder: Mapped[str] = mapped_column(sa.String(255), nullable=False)
    uid: Mapped[int] = mapped_column(sa.Integer, nullable=True)

    __table_args__ = (UniqueConstraint("account_id", "email_id", name="uq_account_email"),)
