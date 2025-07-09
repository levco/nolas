from sqlalchemy import and_, or_

from app.models import Email
from app.repos.base import BaseRepo


class EmailRepo(BaseRepo[Email]):
    """Repository for Email model operations."""

    def __init__(self) -> None:
        super().__init__(Email)

    async def get_by_account_and_email_id(self, account_id: int, email_id: str) -> Email | None:
        """Get email by account and email id."""
        result = await self.execute(self.base_stmt.where(Email.account_id == account_id, Email.email_id == email_id))
        return result.one_or_none()

    async def get_by_account_and_uid_or_email_id(
        self, account_id: int, folder: str, uid: int, email_id: str
    ) -> Email | None:
        """Get email by account and uid or email id."""
        result = await self.execute(
            self.base_stmt.where(
                Email.account_id == account_id,
                or_(
                    and_(Email.uid == uid, Email.folder == folder),
                    Email.email_id == email_id,
                ),
            )
        )
        return result.one_or_none()
