from typing import Any, cast

from fastapi_async_sqlalchemy import db
from sqlalchemy import ScalarResult, select
from sqlalchemy.orm import selectinload

from app.models.account import Account, AccountStatus
from app.repos.base import BaseRepo


class AccountRepo(BaseRepo[Account]):
    """Repository for Account model operations."""

    def __init__(self) -> None:
        super().__init__(Account)

    async def get_by_email(self, email: str) -> Account | None:
        """Get account by email."""
        result = await db.session.execute(select(Account).where(Account.email == email))
        return cast(Account | None, result.scalar_one_or_none())

    async def get_all_active(self) -> ScalarResult[Account]:
        """Get all active accounts."""
        query = self.base_stmt.where(Account.status == AccountStatus.active).options(selectinload(Account.app))
        result = await db.session.execute(query)
        return cast(ScalarResult[Account], result.scalars())

    async def create_account(self, account_data: dict[str, Any]) -> Account:
        """Create a new account."""
        account = Account(**account_data)
        db.session.add(account)
        await db.session.flush()
        return account

    async def update(self, account: Account, update_data: dict[str, Any]) -> Account:
        """Update an account."""
        for key, value in update_data.items():
            setattr(account, key, value)
        await db.session.flush()
        return account
