"""add_microsoft_credentials_to_apps

Revision ID: c7d8e9f0a1b2
Revises: b41ca79fdd06
Create Date: 2026-10-06 12:00:00.000000

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "c7d8e9f0a1b2"
down_revision: Union[str, Sequence[str], None] = "b41ca79fdd06"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("apps", sa.Column("microsoft_client_id", sa.String(length=255), nullable=True))
    op.add_column("apps", sa.Column("microsoft_client_secret", sa.String(length=255), nullable=True))


def downgrade() -> None:
    op.drop_column("apps", "microsoft_client_secret")
    op.drop_column("apps", "microsoft_client_id")
