"""initial_migration

Revision ID: 91fb69797b0a
Revises:
Create Date: 2025-07-14 17:59:06.142787

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "91fb69797b0a"
down_revision: Union[str, Sequence[str], None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "apps",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("uuid", sa.UUID(), server_default=sa.text("uuid_generate_v4()"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("api_key", sa.String(length=255), nullable=False),
        sa.Column("webhook_url", sa.String(length=255), nullable=True),
        sa.Column("webhook_secret", sa.String(length=255), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_apps_uuid"), "apps", ["uuid"], unique=False)
    op.create_table(
        "accounts",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("uuid", sa.UUID(), server_default=sa.text("uuid_generate_v4()"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("app_id", sa.BigInteger(), nullable=False),
        sa.Column("email", sa.String(length=255), nullable=False),
        sa.Column("provider", sa.String(length=50), nullable=False),
        sa.Column("credentials", sa.String(length=255), nullable=False, comment="Encrypted password or token"),
        sa.Column(
            "provider_context", postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'{}'"), nullable=False
        ),
        sa.Column("status", sa.String(length=50), server_default="active", nullable=False),
        sa.ForeignKeyConstraint(["app_id"], ["apps.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("app_id", "email", name="uq_account_app_id_email"),
    )
    op.create_index(op.f("ix_accounts_app_id"), "accounts", ["app_id"], unique=False)
    op.create_index(op.f("ix_accounts_uuid"), "accounts", ["uuid"], unique=False)
    op.create_table(
        "connection_health",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("account_id", sa.Integer(), nullable=False),
        sa.Column("folder", sa.String(length=255), nullable=False),
        sa.Column("last_success_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("consecutive_failures", sa.Integer(), nullable=False),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.ForeignKeyConstraint(["account_id"], ["accounts.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("account_id", "folder"),
    )
    op.create_index(op.f("ix_connection_health_account_id"), "connection_health", ["account_id"], unique=False)
    op.create_table(
        "emails",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("email_id", sa.String(length=255), nullable=False),
        sa.Column("thread_id", sa.String(length=255), nullable=False),
        sa.Column("account_id", sa.Integer(), nullable=False),
        sa.Column("folder", sa.String(length=255), nullable=False),
        sa.Column("uid", sa.Integer(), nullable=True),
        sa.ForeignKeyConstraint(["account_id"], ["accounts.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("account_id", "email_id", name="uq_account_email"),
    )
    op.create_table(
        "oauth2_authorization_requests",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("app_id", sa.BigInteger(), nullable=False),
        sa.Column("client_id", sa.String(length=255), nullable=False),
        sa.Column("account_id", sa.Integer(), nullable=False),
        sa.Column("redirect_uri", sa.String(length=500), nullable=False),
        sa.Column("state", sa.String(length=255), nullable=False),
        sa.Column("scope", sa.String(length=255), nullable=True),
        sa.Column("status", sa.String(length=50), server_default="pending", nullable=False),
        sa.Column("code", sa.String(length=255), nullable=False),
        sa.Column("code_used", sa.Boolean(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "request_metadata", postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'{}'"), nullable=False
        ),
        sa.ForeignKeyConstraint(["account_id"], ["accounts.id"]),
        sa.ForeignKeyConstraint(["app_id"], ["apps.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_oauth2_authorization_requests_account_id"),
        "oauth2_authorization_requests",
        ["account_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_oauth2_authorization_requests_app_id"), "oauth2_authorization_requests", ["app_id"], unique=False
    )
    op.create_index(
        op.f("ix_oauth2_authorization_requests_code"), "oauth2_authorization_requests", ["code"], unique=False
    )
    op.create_table(
        "uid_tracking",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("account_id", sa.Integer(), nullable=False),
        sa.Column("folder", sa.String(length=255), nullable=False),
        sa.Column("last_seen_uid", sa.BigInteger(), nullable=False),
        sa.Column("last_checked_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(["account_id"], ["accounts.id"]),
        sa.UniqueConstraint("account_id", "folder"),
    )
    op.create_index(op.f("ix_uid_tracking_account_id"), "uid_tracking", ["account_id"], unique=False)
    op.create_table(
        "webhook_logs",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("app_id", sa.BigInteger(), nullable=False),
        sa.Column("account_id", sa.Integer(), nullable=False),
        sa.Column("folder", sa.String(length=255), nullable=False),
        sa.Column("uid", sa.BigInteger(), nullable=False),
        sa.Column("webhook_url", sa.Text(), nullable=False),
        sa.Column("status_code", sa.Integer(), nullable=True),
        sa.Column("response_body", sa.Text(), nullable=True),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("delivered_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["account_id"], ["accounts.id"]),
        sa.ForeignKeyConstraint(["app_id"], ["apps.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_webhook_logs_account_id"), "webhook_logs", ["account_id"], unique=False)
    op.create_index(op.f("ix_webhook_logs_app_id"), "webhook_logs", ["app_id"], unique=False)


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index(op.f("ix_webhook_logs_app_id"), table_name="webhook_logs")
    op.drop_index(op.f("ix_webhook_logs_account_id"), table_name="webhook_logs")
    op.drop_table("webhook_logs")
    op.drop_index(op.f("ix_uid_tracking_account_id"), table_name="uid_tracking")
    op.drop_table("uid_tracking")
    op.drop_index(op.f("ix_oauth2_authorization_requests_code"), table_name="oauth2_authorization_requests")
    op.drop_index(op.f("ix_oauth2_authorization_requests_app_id"), table_name="oauth2_authorization_requests")
    op.drop_index(op.f("ix_oauth2_authorization_requests_account_id"), table_name="oauth2_authorization_requests")
    op.drop_table("oauth2_authorization_requests")
    op.drop_table("emails")
    op.drop_index(op.f("ix_connection_health_account_id"), table_name="connection_health")
    op.drop_table("connection_health")
    op.drop_index(op.f("ix_accounts_uuid"), table_name="accounts")
    op.drop_index(op.f("ix_accounts_app_id"), table_name="accounts")
    op.drop_table("accounts")
    op.drop_index(op.f("ix_apps_uuid"), table_name="apps")
    op.drop_table("apps")
