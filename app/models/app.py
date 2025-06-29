import sqlalchemy as sa
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base, TimestampMixin, WithUUID


class App(Base, WithUUID, TimestampMixin):
    """App model for storing app configurations."""

    __tablename__ = "apps"

    name: Mapped[str] = mapped_column(sa.String(255), nullable=False)
    api_key: Mapped[str] = mapped_column(sa.String(255), nullable=False)
    webhook_url: Mapped[str] = mapped_column(sa.String(255), nullable=True)
