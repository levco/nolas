from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Account


class AccountRepo:
    """Repository for Account model operations."""

    def __init__(self, session: AsyncSession):
        self._session = session

    async def get_by_id(self, account_id: int) -> Account | None:
        """Get account by ID."""
        result = await self._session.execute(select(Account).where(Account.id == account_id))
        return result.scalar_one_or_none()

    async def get_by_email(self, email: str) -> Account | None:
        """Get account by email."""
        result = await self._session.execute(select(Account).where(Account.email == email))
        return result.scalar_one_or_none()

    async def get_all_active(self) -> list[Account]:
        """Get all active accounts."""
        result = await self._session.execute(select(Account).where(Account.is_active.is_(True)))
        return list(result.scalars().all())

    async def create(self, account_data: dict[str, Any]) -> Account:
        """Create a new account."""
        account = Account(**account_data)
        self._session.add(account)
        await self._session.flush()
        return account

    async def update(self, account: Account, update_data: dict[str, Any]) -> Account:
        """Update an account."""
        for key, value in update_data.items():
            setattr(account, key, value)
        await self._session.flush()
        return account

    async def delete(self, account: Account) -> None:
        """Delete an account."""
        await self._session.delete(account)
        await self._session.flush()

    async def deactivate(self, account: Account) -> Account:
        """Deactivate an account."""
        account.is_active = False
        await self._session.flush()
        return account
