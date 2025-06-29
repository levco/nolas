from typing import cast

from fastapi_async_sqlalchemy import db
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert

from app.models import ConnectionHealth
from app.repos.base import BaseRepo


class ConnectionHealthRepo(BaseRepo[ConnectionHealth]):
    """Repository for ConnectionHealth model operations."""

    def __init__(self) -> None:
        super().__init__(ConnectionHealth)

    async def record_success(self, account_id: int, folder: str) -> ConnectionHealth:
        """Record a successful connection."""
        stmt = insert(ConnectionHealth).values(
            account_id=account_id, folder=folder, consecutive_failures=0, last_error=None, is_active=True
        )

        stmt = stmt.on_conflict_do_update(
            index_elements=["account_id", "folder"],
            set_={
                "consecutive_failures": 0,
                "last_error": None,
                "is_active": True,
                "last_success_at": stmt.excluded.last_success_at,
            },
        )

        await db.session.execute(stmt)
        await db.session.flush()

        result = await db.session.execute(
            select(ConnectionHealth).where(ConnectionHealth.account_id == account_id, ConnectionHealth.folder == folder)
        )
        connection_health = cast(ConnectionHealth | None, result.scalar_one_or_none())
        if connection_health is None:
            raise ValueError(f"Failed to create/update connection health for {account_id}/{folder}")
        return connection_health

    async def record_failure(self, account_id: int, folder: str, error_message: str) -> ConnectionHealth:
        """Record a connection failure."""
        # First, get current record to increment failures
        current_result = await db.session.execute(
            select(ConnectionHealth).where(ConnectionHealth.account_id == account_id, ConnectionHealth.folder == folder)
        )
        current = cast(ConnectionHealth | None, current_result.scalar_one_or_none())
        current_failures = current.consecutive_failures if current else 0
        new_failures = current_failures + 1

        stmt = insert(ConnectionHealth).values(
            account_id=account_id,
            folder=folder,
            consecutive_failures=new_failures,
            last_error=error_message,
            is_active=new_failures < 5,  # Deactivate after 5 consecutive failures
        )

        stmt = stmt.on_conflict_do_update(
            index_elements=["account_id", "folder"],
            set_={"consecutive_failures": new_failures, "last_error": error_message, "is_active": new_failures < 5},
        )

        await db.session.execute(stmt)
        await db.session.flush()

        result = await db.session.execute(
            select(ConnectionHealth).where(ConnectionHealth.account_id == account_id, ConnectionHealth.folder == folder)
        )
        connection_health = cast(ConnectionHealth | None, result.scalar_one_or_none())
        if connection_health is None:
            raise ValueError(f"Failed to create/update connection health for {account_id}/{folder}")
        return connection_health

    async def get_by_account_folder(self, account_id: int, folder: str) -> ConnectionHealth | None:
        """Get connection health by account and folder."""
        result = await db.session.execute(
            select(ConnectionHealth).where(ConnectionHealth.account_id == account_id, ConnectionHealth.folder == folder)
        )
        return cast(ConnectionHealth | None, result.scalar_one_or_none())

    async def get_failed_connections(self, max_failures: int = 5) -> list[ConnectionHealth]:
        """Get connections with too many failures."""
        result = await db.session.execute(
            select(ConnectionHealth).where(ConnectionHealth.consecutive_failures >= max_failures)
        )
        return list(result.scalars().all())

    async def get_inactive_connections(self) -> list[ConnectionHealth]:
        """Get inactive connections."""
        result = await db.session.execute(select(ConnectionHealth).where(ConnectionHealth.is_active.is_(False)))
        return list(result.scalars().all())

    async def get_all_for_account(self, account_id: int) -> list[ConnectionHealth]:
        """Get all connection health records for an account."""
        result = await db.session.execute(select(ConnectionHealth).where(ConnectionHealth.account_id == account_id))
        return list(result.scalars().all())
