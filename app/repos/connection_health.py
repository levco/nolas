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

        await self._db.session.execute(stmt)
        await self._db.session.flush()

        result = await self.execute(
            self.base_stmt.where(ConnectionHealth.account_id == account_id, ConnectionHealth.folder == folder)
        )
        connection_health = result.one_or_none()
        if connection_health is None:
            raise ValueError(f"Failed to create/update connection health for {account_id}/{folder}")
        return connection_health

    async def record_failure(self, account_id: int, folder: str, error_message: str) -> ConnectionHealth:
        """Record a connection failure."""
        # First, get current record to increment failures
        current_result = await self.execute(
            self.base_stmt.where(ConnectionHealth.account_id == account_id, ConnectionHealth.folder == folder)
        )
        current = current_result.one_or_none()
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

        await self._db.session.execute(stmt)
        await self._db.session.flush()

        result = await self.execute(
            self.base_stmt.where(ConnectionHealth.account_id == account_id, ConnectionHealth.folder == folder)
        )
        connection_health = result.one_or_none()
        if connection_health is None:
            raise ValueError(f"Failed to create/update connection health for {account_id}/{folder}")
        return connection_health
