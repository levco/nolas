from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import ConnectionHealth


class ConnectionHealthRepo:
    """Repository for ConnectionHealth model operations."""

    def __init__(self, session: AsyncSession):
        self._session = session

    async def record_success(self, account_email: str, folder: str) -> ConnectionHealth:
        """Record a successful connection."""
        stmt = insert(ConnectionHealth).values(
            account_email=account_email, folder=folder, consecutive_failures=0, last_error=None, is_active=True
        )

        stmt = stmt.on_conflict_do_update(
            index_elements=["account_email", "folder"],
            set_={
                "consecutive_failures": 0,
                "last_error": None,
                "is_active": True,
                "last_success_at": stmt.excluded.last_success_at,
            },
        )

        await self._session.execute(stmt)
        await self._session.flush()

        result = await self.get_by_account_folder(account_email, folder)
        if result is None:
            raise ValueError(f"Failed to create/update connection health for {account_email}/{folder}")
        return result

    async def record_failure(self, account_email: str, folder: str, error_message: str) -> ConnectionHealth:
        """Record a connection failure."""
        # First, get current record to increment failures
        current = await self.get_by_account_folder(account_email, folder)
        current_failures = current.consecutive_failures if current else 0
        new_failures = current_failures + 1

        stmt = insert(ConnectionHealth).values(
            account_email=account_email,
            folder=folder,
            consecutive_failures=new_failures,
            last_error=error_message,
            is_active=new_failures < 5,  # Deactivate after 5 consecutive failures
        )

        stmt = stmt.on_conflict_do_update(
            index_elements=["account_email", "folder"],
            set_={"consecutive_failures": new_failures, "last_error": error_message, "is_active": new_failures < 5},
        )

        await self._session.execute(stmt)
        await self._session.flush()

        result = await self.get_by_account_folder(account_email, folder)
        if result is None:
            raise ValueError(f"Failed to create/update connection health for {account_email}/{folder}")
        return result

    async def get_by_account_folder(self, account_email: str, folder: str) -> ConnectionHealth | None:
        """Get connection health by account and folder."""
        result = await self._session.execute(
            select(ConnectionHealth).where(
                ConnectionHealth.account_email == account_email,
                ConnectionHealth.folder == folder,
            )
        )
        return result.scalar_one_or_none()

    async def get_failed_connections(self, max_failures: int = 5) -> list[ConnectionHealth]:
        """Get connections with too many failures."""
        result = await self._session.execute(
            select(ConnectionHealth).where(ConnectionHealth.consecutive_failures >= max_failures)
        )
        return list(result.scalars().all())

    async def get_inactive_connections(self) -> list[ConnectionHealth]:
        """Get inactive connections."""
        result = await self._session.execute(select(ConnectionHealth).where(ConnectionHealth.is_active.is_(False)))
        return list(result.scalars().all())

    async def get_all_for_account(self, account_email: str) -> list[ConnectionHealth]:
        """Get all connection health records for an account."""
        result = await self._session.execute(
            select(ConnectionHealth).where(ConnectionHealth.account_email == account_email)
        )
        return list(result.scalars().all())
