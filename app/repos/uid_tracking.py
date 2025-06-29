from typing import cast

from fastapi_async_sqlalchemy import db
from sqlalchemy import delete, select
from sqlalchemy.sql import func

from app.models import UidTracking
from app.repos.base import BaseRepo


class UidTrackingRepo(BaseRepo[UidTracking]):
    """Repository for UidTracking model operations."""

    def __init__(self) -> None:
        super().__init__(UidTracking)

    async def get_by_account_folder(self, account_id: int, folder: str) -> UidTracking | None:
        """Get UID tracking record by account and folder."""
        result = await db.session.execute(
            select(UidTracking).where(UidTracking.account_id == account_id, UidTracking.folder == folder)
        )
        return cast(UidTracking | None, result.scalar_one_or_none())

    async def get_last_seen_uid(self, account_id: int, folder: str) -> int:
        """Get the last seen UID for an account/folder combination."""
        result = await db.session.execute(
            select(UidTracking.last_seen_uid).where(UidTracking.account_id == account_id, UidTracking.folder == folder)
        )
        uid = result.scalar_one_or_none()
        return uid if uid is not None else 0

    async def update_last_seen_uid(self, account_id: int, folder: str, uid: int) -> UidTracking:
        """Update the last seen UID for an account/folder combination."""
        tracking_result = await db.session.execute(
            select(UidTracking).where(UidTracking.account_id == account_id, UidTracking.folder == folder)
        )
        tracking = cast(UidTracking | None, tracking_result.scalar_one_or_none())

        if tracking:
            # Only update if the new UID is greater
            if uid > tracking.last_seen_uid:
                tracking.last_seen_uid = uid
                tracking.last_checked_at = func.now()
        else:
            tracking = UidTracking(account_id=account_id, folder=folder, last_seen_uid=uid)
            db.session.add(tracking)

        return tracking

    async def get_all_for_account(self, account_id: int) -> list[UidTracking]:
        """Get all UID tracking records for an account."""
        result = await db.session.execute(select(UidTracking).where(UidTracking.account_id == account_id))
        return list(result.scalars().all())

    async def cleanup_old_records(self, days: int = 90) -> int:
        """Clean up old tracking records."""
        cutoff_date = func.now() - func.interval(f"{days} days")
        result = await db.session.execute(delete(UidTracking).where(UidTracking.last_checked_at < cutoff_date))
        await db.session.flush()
        return cast(int, result.rowcount)
