from datetime import datetime
from enum import Enum
from typing import Any

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base
from .decorators.types import EnumStringType


class MailboxThreadIndexPhase(Enum):
    backfill = "backfill"
    catchup = "catchup"
    ready = "ready"


class MailboxThread(Base):
    """Searchable thread metadata; message bodies and credentials stay with the provider."""

    __tablename__ = "mailbox_threads"

    account_id: Mapped[int] = mapped_column(
        sa.BigInteger, sa.ForeignKey("accounts.id", ondelete="CASCADE"), nullable=False
    )
    thread_id: Mapped[str] = mapped_column(sa.String(255), nullable=False)
    last_message_id: Mapped[str] = mapped_column(sa.String(255), nullable=False)
    last_message_date: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True), nullable=False)
    subject: Mapped[str] = mapped_column(sa.Text, nullable=False)
    folders: Mapped[list[str]] = mapped_column(ARRAY(sa.String), nullable=False)
    participants: Mapped[list[str]] = mapped_column(ARRAY(sa.String), nullable=False)
    senders: Mapped[list[str]] = mapped_column(ARRAY(sa.String), nullable=False)
    recipients: Mapped[list[str]] = mapped_column(ARRAY(sa.String), nullable=False)
    cc: Mapped[list[str]] = mapped_column(ARRAY(sa.String), nullable=False)
    bcc: Mapped[list[str]] = mapped_column(ARRAY(sa.String), nullable=False)
    unread: Mapped[bool] = mapped_column(sa.Boolean, nullable=False)
    starred: Mapped[bool] = mapped_column(sa.Boolean, nullable=False)
    has_attachment: Mapped[bool] = mapped_column(sa.Boolean, nullable=False)
    message_ids: Mapped[list[str]] = mapped_column(ARRAY(sa.String), nullable=False, server_default="{}")
    cached_thread: Mapped[dict[str, Any] | None] = mapped_column(JSONB)

    __table_args__ = (
        sa.UniqueConstraint("account_id", "thread_id", name="uq_mailbox_thread_account_thread"),
        sa.Index("ix_mailbox_threads_page", "account_id", last_message_date.desc(), thread_id.desc()),
        sa.Index("ix_mailbox_threads_messages", "message_ids", postgresql_using="gin"),
    )


class MailboxThreadIndexState(Base):
    __tablename__ = "mailbox_thread_index_states"

    account_id: Mapped[int] = mapped_column(
        sa.BigInteger, sa.ForeignKey("accounts.id", ondelete="CASCADE"), nullable=False, unique=True
    )
    phase: Mapped[MailboxThreadIndexPhase] = mapped_column(
        EnumStringType(MailboxThreadIndexPhase), nullable=False, default=MailboxThreadIndexPhase.backfill
    )
    page_token: Mapped[str | None] = mapped_column(sa.Text)
    history_id: Mapped[str | None] = mapped_column(sa.String(255))
    provider_state: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
