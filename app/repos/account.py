from typing import cast

from fastapi_async_sqlalchemy import db
from sqlalchemy import ScalarResult, text
from sqlalchemy.orm import selectinload

from app.models.account import Account, AccountProvider, AccountStatus
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

    async def get_by_app_and_email(self, app_id: int, email: str) -> Account | None:
        """Get account by app and email."""
        query = self.base_stmt.where(Account.app_id == app_id, Account.email == email)
        result = await self.execute(query)
        return result.one_or_none()

    async def get_all_by_email_and_provider(self, email: str, provider: AccountProvider) -> list[Account]:
        """Get all accounts (across apps) for an email address and provider."""
        query = self.base_stmt.where(Account.email == email, Account.provider == provider).options(
            selectinload(Account.app)
        )
        result = await self.execute(query)
        return list(result.all())

    async def get_by_subscription_id(self, subscription_id: str) -> Account | None:
        """Get account by Microsoft Graph subscription id stored in provider_context."""
        query = self.base_stmt.where(Account.provider_context["subscription_id"].astext == subscription_id).options(
            selectinload(Account.app)
        )
        result = await self.execute(query)
        return result.one_or_none()

    async def get_all_active_by_providers(self, providers: list[AccountProvider]) -> list[Account]:
        """Get all active accounts for the given providers."""
        query = self.base_stmt.where(Account.status == AccountStatus.active, Account.provider.in_(providers)).options(
            selectinload(Account.app)
        )
        result = await self.execute(query)
        return list(result.all())

    async def get_all_active(self) -> ScalarResult[Account]:
        """Get all active accounts."""
        query = self.base_stmt.where(Account.status == AccountStatus.active).options(selectinload(Account.app))
        result = await db.session.execute(query)
        return cast(ScalarResult[Account], result.scalars())

    # Advisory lock namespace for refresh-token redemption (avoids collisions with other locks).
    _REFRESH_LOCK_NAMESPACE = 0x4E4F4C41  # "NOLA"

    async def acquire_refresh_lock(self, account_id: int) -> None:
        """Blocks until this process holds the cross-replica refresh lock for the account.

        Transaction-scoped (pg_advisory_xact_lock): released automatically at commit/rollback.
        """
        await db.session.execute(
            text("SELECT pg_advisory_xact_lock(:ns, :key)"),
            {"ns": self._REFRESH_LOCK_NAMESPACE, "key": account_id},
        )

    async def refresh_from_db(self, account: Account) -> Account:
        """Re-read the account row, discarding stale in-memory state."""
        await db.session.refresh(account)
        return account

    async def mark_as_active(self, account: Account) -> Account:
        """Mark an account as active."""
        account.status = AccountStatus.active
        await db.session.flush()
        return account
