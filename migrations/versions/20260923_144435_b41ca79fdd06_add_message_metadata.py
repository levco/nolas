"""add_message_metadata

Revision ID: b41ca79fdd06
Revises: b8f3e1c2d4a5
Create Date: 2026-09-23 14:44:35.486794

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "b41ca79fdd06"
down_revision: Union[str, Sequence[str], None] = "b8f3e1c2d4a5"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column("emails", sa.Column("message_metadata", postgresql.JSONB(astext_type=sa.Text()), nullable=True))
    op.create_index("ix_emails_message_metadata", "emails", ["message_metadata"], unique=False, postgresql_using="gin")


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index("ix_emails_message_metadata", table_name="emails", postgresql_using="gin")
    op.drop_column("emails", "message_metadata")
