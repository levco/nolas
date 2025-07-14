from sqlalchemy.sql import func

from app.models import UidTracking
from app.repos.base import BaseRepo


class UidTrackingRepo(BaseRepo[UidTracking]):
    """Repository for UidTracking model operations."""

    def __init__(self) -> None:
        super().__init__(UidTracking)

    async def get_last_seen_uid(self, account_id: int, folder: str) -> int | None:
        """Get the last seen UID for an account/folder combination."""
        result = await self.execute(
            self.base_stmt.where(UidTracking.account_id == account_id, UidTracking.folder == folder)
        )
        uid_tracking = result.one_or_none()
        return uid_tracking.last_seen_uid if uid_tracking else None

    async def update_last_seen_uid(self, account_id: int, folder: str, uid: int) -> UidTracking:
        """Update the last seen UID for an account/folder combination."""
        tracking_result = await self.execute(
            self.base_stmt.where(UidTracking.account_id == account_id, UidTracking.folder == folder)
        )
        tracking = tracking_result.one_or_none()

        if tracking:
            # Only update if the new UID is greater
            if uid > tracking.last_seen_uid:
                tracking.last_seen_uid = uid
                tracking.last_checked_at = func.now()
        else:
            tracking = UidTracking(account_id=account_id, folder=folder, last_seen_uid=uid)
            await self.add(tracking)

        return tracking

    async def delete_all_by_account(self, account_id: int) -> int:
        """Delete all uid_tracking records for a specific account."""
        result = await self.execute(self.base_stmt.where(UidTracking.account_id == account_id))
        uid_tracking_records = result.all()

        count = len(uid_tracking_records)
        for record in uid_tracking_records:
            await self.delete(record)

        await self.flush()
        return count
