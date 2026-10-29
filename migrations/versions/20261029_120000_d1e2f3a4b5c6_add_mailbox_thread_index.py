"""Add metadata-only mailbox thread index and resumable backfill state."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "d1e2f3a4b5c6"
down_revision = "c7d8e9f0a1b2"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "mailbox_thread_index_states",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column(
            "account_id", sa.BigInteger(), sa.ForeignKey("accounts.id", ondelete="CASCADE"), nullable=False, unique=True
        ),
        sa.Column("phase", sa.String(50), nullable=False),
        sa.Column("page_token", sa.Text()),
        sa.Column("history_id", sa.String(255)),
        sa.Column("provider_state", postgresql.JSONB()),
    )
    op.create_table(
        "mailbox_threads",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("account_id", sa.BigInteger(), sa.ForeignKey("accounts.id", ondelete="CASCADE"), nullable=False),
        sa.Column("thread_id", sa.String(255), nullable=False),
        sa.Column("last_message_id", sa.String(255), nullable=False),
        sa.Column("last_message_date", sa.DateTime(timezone=True), nullable=False),
        sa.Column("subject", sa.Text(), nullable=False),
        *[
            sa.Column(name, postgresql.ARRAY(sa.String()), nullable=False)
            for name in ("folders", "participants", "senders", "recipients", "cc", "bcc")
        ],
        sa.Column("unread", sa.Boolean(), nullable=False),
        sa.Column("starred", sa.Boolean(), nullable=False),
        sa.Column("has_attachment", sa.Boolean(), nullable=False),
        sa.Column("message_ids", postgresql.ARRAY(sa.String()), nullable=False, server_default="{}"),
        sa.Column("cached_thread", postgresql.JSONB()),
        sa.UniqueConstraint("account_id", "thread_id", name="uq_mailbox_thread_account_thread"),
    )
    op.create_index(
        "ix_mailbox_threads_page",
        "mailbox_threads",
        ["account_id", sa.text("last_message_date DESC"), sa.text("thread_id DESC")],
    )
    op.create_index("ix_mailbox_threads_messages", "mailbox_threads", ["message_ids"], postgresql_using="gin")


def downgrade() -> None:
    op.drop_table("mailbox_threads")
    op.drop_table("mailbox_thread_index_states")
