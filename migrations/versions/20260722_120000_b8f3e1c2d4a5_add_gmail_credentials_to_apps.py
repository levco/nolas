"""add_gmail_credentials_to_apps

Revision ID: b8f3e1c2d4a5
Revises: 92082863c011
Create Date: 2026-07-22 12:00:00.000000

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "b8f3e1c2d4a5"
down_revision: Union[str, Sequence[str], None] = "92082863c011"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column("apps", sa.Column("gmail_client_id", sa.String(length=255), nullable=True))
    op.add_column("apps", sa.Column("gmail_client_secret", sa.String(length=255), nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column("apps", "gmail_client_secret")
    op.drop_column("apps", "gmail_client_id")
