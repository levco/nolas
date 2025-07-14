from datetime import UTC, datetime, timedelta
from enum import Enum
from typing import TYPE_CHECKING, Any

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .base import Base, TimestampMixin
from .decorators.types import EnumStringType

if TYPE_CHECKING:
    from .account import Account
    from .app import App


class OAuth2RequestStatus(Enum):
    pending = "pending"
    authorized = "authorized"
    denied = "denied"
    expired = "expired"


class OAuth2AuthorizationRequest(Base, TimestampMixin):
    """OAuth2 authorization request model for storing pending authorization requests."""

    __tablename__ = "oauth2_authorization_requests"

    id: Mapped[int] = mapped_column(primary_key=True)
    app_id: Mapped[int] = mapped_column(sa.ForeignKey("apps.id"), nullable=False, index=True)
    client_id: Mapped[str] = mapped_column(sa.String(255), nullable=False)
    account_id: Mapped[int] = mapped_column(sa.ForeignKey("accounts.id"), nullable=False, index=True)
    redirect_uri: Mapped[str] = mapped_column(sa.String(500), nullable=False)
    state: Mapped[str] = mapped_column(sa.String(255), nullable=False)
    scope: Mapped[str] = mapped_column(sa.String(255), nullable=True)
    status: Mapped[OAuth2RequestStatus] = mapped_column(
        EnumStringType(OAuth2RequestStatus), nullable=False, server_default=OAuth2RequestStatus.pending.name
    )
    code: Mapped[str] = mapped_column(sa.String(255), nullable=False, index=True)
    code_used: Mapped[bool] = mapped_column(sa.Boolean, nullable=False, default=False)
    expires_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), nullable=False, default=lambda: datetime.now(UTC) + timedelta(minutes=10)
    )
    request_metadata: Mapped[dict[str, Any]] = mapped_column(JSONB(), nullable=False, server_default=sa.text("'{}'"))

    app: Mapped["App"] = relationship("App")
    account: Mapped["Account"] = relationship("Account")

    def is_valid(self) -> bool:
        """Check if the authorization request is valid."""
        return not self.code_used and datetime.now(UTC) < self.expires_at

    def __repr__(self) -> str:
        return f"<OAuth2AuthorizationRequest(client_id='{self.client_id}', state='{self.state}', status='{self.status.name}')>"
