from datetime import datetime
from typing import Any
from uuid import UUID

import sqlalchemy as sa
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    """Base class for all SQLAlchemy models."""

    id: Mapped[int] = mapped_column(sa.BigInteger(), primary_key=True)

    def to_dict(self) -> dict[str, Any]:
        """Convert model instance to dictionary."""
        return {c.name: getattr(self, c.name) for c in self.__table__.columns}


class WithUUID:
    """Base class for all SQLAlchemy models with a UUID primary key."""

    uuid: Mapped[UUID] = mapped_column(sa.UUID(as_uuid=True), index=True, server_default=sa.text("uuid_generate_v4()"))


class TimestampMixin:
    """Mixin for created_at and updated_at timestamps."""

    created_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), server_default=sa.func.now(), onupdate=sa.func.now(), nullable=False
    )
