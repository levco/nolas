from enum import Enum
from typing import TYPE_CHECKING, Any

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.schema import UniqueConstraint

from .base import Base, TimestampMixin, WithUUID
from .decorators.types import EnumStringType

if TYPE_CHECKING:
    from .app import App


class AccountProvider(Enum):
    imap = "imap"


class AccountStatus(Enum):
    active = "active"
    inactive = "inactive"
    pending = "pending"


class Account(Base, WithUUID, TimestampMixin):
    """Account model for storing email account configurations."""

    __tablename__ = "accounts"

    id: Mapped[int] = mapped_column(primary_key=True)
    app_id: Mapped[int] = mapped_column(sa.ForeignKey("apps.id"), nullable=False, index=True)
    email: Mapped[str] = mapped_column(sa.String(255), nullable=False)
    provider: Mapped[AccountProvider] = mapped_column(EnumStringType(AccountProvider), nullable=False)
    credentials: Mapped[str] = mapped_column(sa.String(255), nullable=False, comment="Encrypted password or token")
    provider_context: Mapped[dict[str, Any]] = mapped_column(JSONB(), nullable=False, server_default=sa.text("'{}'"))
    status: Mapped[AccountStatus] = mapped_column(
        EnumStringType(AccountStatus), nullable=False, server_default=AccountStatus.active.name
    )

    app: Mapped["App"] = relationship("App")

    __table_args__ = (UniqueConstraint("app_id", "email", name="uq_account_app_id_email"),)

    def __repr__(self) -> str:
        return f"<Account(email='{self.email}', provider='{self.provider.name}')>"
