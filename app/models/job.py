from datetime import datetime
from enum import Enum
from typing import Any

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base, TimestampMixin
from .decorators.types import EnumStringType


class JobType(Enum):
    google_notification = "google_notification"
    microsoft_notification = "microsoft_notification"
    subscription_renewal = "subscription_renewal"
    webhook_delivery = "webhook_delivery"


class JobStatus(Enum):
    pending = "pending"
    processing = "processing"
    completed = "completed"
    failed = "failed"


class Job(Base, TimestampMixin):
    """Generic durable job queue row."""

    __tablename__ = "jobs"

    type: Mapped[JobType] = mapped_column(EnumStringType(JobType), nullable=False, index=True)
    status: Mapped[JobStatus] = mapped_column(
        EnumStringType(JobStatus),
        nullable=False,
        index=True,
        server_default=JobStatus.pending.name,
    )
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB(), nullable=False, server_default=sa.text("'{}'"))
    attempts: Mapped[int] = mapped_column(sa.Integer(), nullable=False, server_default=sa.text("0"))
    max_attempts: Mapped[int] = mapped_column(sa.Integer(), nullable=False, server_default=sa.text("5"))
    available_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), nullable=False, index=True, server_default=sa.func.now()
    )
    completed_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True), nullable=True)
    locked_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True), nullable=True)
    locked_by: Mapped[str | None] = mapped_column(sa.String(length=255), nullable=True)
    last_error: Mapped[str | None] = mapped_column(sa.Text(), nullable=True)
