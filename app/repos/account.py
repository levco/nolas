from typing import cast

from fastapi_async_sqlalchemy import db
from sqlalchemy import ScalarResult
from sqlalchemy.orm import selectinload

from app.models.account import Account, AccountStatus
from app.repos.base import BaseRepo


class AccountRepo(BaseRepo[Account]):
    """Repository for Account model operations."""

    def __init__(self) -> None:
        super().__init__(Account)

    async def get_by_app_and_uuid(self, app_id: int, uuid: str) -> Account | None:
        """Get account by app and uuid."""
        query = self.base_stmt.where(Account.app_id == app_id, Account.uuid == uuid)
        result = await self.execute(query)
        return result.one_or_none()

    async def get_by_email(self, email: str) -> Account | None:
        """Get account by email."""
        query = self.base_stmt.where(Account.email == email)
        result = await self.execute(query)
        return result.one_or_none()

    async def get_all_active(self) -> ScalarResult[Account]:
        """Get all active accounts."""
        query = self.base_stmt.where(Account.status == AccountStatus.active).options(selectinload(Account.app))
        result = await db.session.execute(query)
        return cast(ScalarResult[Account], result.scalars())

    async def mark_as_active(self, account: Account) -> Account:
        """Mark an account as active."""
        account.status = AccountStatus.active
        await db.session.flush()
        return account
