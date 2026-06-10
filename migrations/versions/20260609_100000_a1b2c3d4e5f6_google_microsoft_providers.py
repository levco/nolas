"""google_microsoft_providers

Revision ID: a1b2c3d4e5f6
Revises: 91fb69797b0a
Create Date: 2026-06-09 10:00:00.000000

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "a1b2c3d4e5f6"
down_revision: Union[str, Sequence[str], None] = "91fb69797b0a"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    # OAuth refresh tokens (Microsoft especially) exceed 255 chars even before encryption.
    op.alter_column(
        "accounts",
        "credentials",
        existing_type=sa.String(length=255),
        type_=sa.Text(),
        existing_nullable=False,
        comment="Encrypted password (imap) or refresh token (google/microsoft)",
    )
    # Generic (non-IMAP) webhook deliveries have no folder/uid.
    op.alter_column("webhook_logs", "folder", existing_type=sa.String(length=255), nullable=True)
    op.alter_column("webhook_logs", "uid", existing_type=sa.BigInteger(), nullable=True)
    op.add_column(
        "apps",
        sa.Column(
            "grant_webhook_url",
            sa.String(length=255),
            nullable=True,
            comment="Destination for grant.* lifecycle events; falls back to webhook_url",
        ),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column("apps", "grant_webhook_url")
    op.alter_column("webhook_logs", "uid", existing_type=sa.BigInteger(), nullable=False)
    op.alter_column("webhook_logs", "folder", existing_type=sa.String(length=255), nullable=False)
    op.alter_column(
        "accounts",
        "credentials",
        existing_type=sa.Text(),
        type_=sa.String(length=255),
        existing_nullable=False,
    )
