from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql import func

from app.models import UidTracking


class UidTrackingRepo:
    """Repository for UidTracking model operations."""

    def __init__(self, session: AsyncSession):
        self._session = session

    async def get_by_account_folder(self, account_email: str, folder: str) -> UidTracking | None:
        """Get UID tracking record by account and folder."""
        result = await self._session.execute(
            select(UidTracking).where(
                UidTracking.account_email == account_email,
                UidTracking.folder == folder,
            )
        )
        return result.scalar_one_or_none()

    async def get_last_seen_uid(self, account_email: str, folder: str) -> int:
        """Get the last seen UID for an account/folder combination."""
        result = await self._session.execute(
            select(UidTracking.last_seen_uid).where(
                UidTracking.account_email == account_email, UidTracking.folder == folder
            )
        )
        uid = result.scalar_one_or_none()
        return uid if uid is not None else 0

    async def update_last_seen_uid(self, account_email: str, folder: str, uid: int) -> UidTracking:
        """Update the last seen UID for an account/folder combination."""
        tracking = await self.get_by_account_folder(account_email, folder)

        if tracking:
            # Only update if the new UID is greater
            if uid > tracking.last_seen_uid:
                tracking.last_seen_uid = uid
                tracking.last_checked_at = func.now()
        else:
            tracking = UidTracking(
                account_email=account_email,
                folder=folder,
                last_seen_uid=uid,
            )
            self._session.add(tracking)

        await self._session.flush()
        return tracking

    async def get_all_for_account(self, account_email: str) -> list[UidTracking]:
        """Get all UID tracking records for an account."""
        result = await self._session.execute(select(UidTracking).where(UidTracking.account_email == account_email))
        return list(result.scalars().all())

    async def cleanup_old_records(self, days: int = 90) -> int:
        """Clean up old tracking records."""
        cutoff_date = func.now() - func.interval(f"{days} days")
        result = await self._session.execute(delete(UidTracking).where(UidTracking.last_checked_at < cutoff_date))
        await self._session.flush()
        return result.rowcount
