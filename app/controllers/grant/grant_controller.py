from app.models.account import Account, AccountStatus
from app.repos.account import AccountRepo
from app.repos.uid_tracking import UidTrackingRepo


class GrantController:
    """Controller for grant operations."""

    def __init__(self, account_repo: AccountRepo, uid_tracking_repo: UidTrackingRepo) -> None:
        self.account_repo = account_repo
        self.uid_tracking_repo = uid_tracking_repo

    async def delete_grant(self, account: Account) -> None:
        """
        Delete a grant by setting account status to inactive and removing uid_tracking records.

        Args:
            account: The account to delete
        """
        await self.account_repo.update(account, {"status": AccountStatus.inactive})
        await self.uid_tracking_repo.delete_all_by_account(account.id)
