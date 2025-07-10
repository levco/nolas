from uuid import UUID

from app.models.app import App
from app.repos.base import BaseRepo


class AppRepo(BaseRepo[App]):
    """App repository."""

    def __init__(self) -> None:
        super().__init__(App)

    async def get_by_api_key(self, api_key: str) -> App | None:
        """Get app by API key."""
        result = await self.execute(self.base_stmt.where(App.api_key == api_key))
        return result.one_or_none()

    async def get_by_uuid(self, uuid: UUID) -> App | None:
        """Get app by UUID."""
        result = await self.execute(self.base_stmt.where(App.uuid == uuid))
        return result.one_or_none()
