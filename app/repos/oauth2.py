from datetime import datetime

from fastapi_async_sqlalchemy import db
from sqlalchemy import and_
from sqlalchemy.orm import selectinload

from app.models.oauth2 import OAuth2AuthorizationRequest, OAuth2RequestStatus
from app.repos.base import BaseRepo


class OAuth2AuthorizationRequestRepo(BaseRepo[OAuth2AuthorizationRequest]):
    """Repository for OAuth2 authorization request operations."""

    def __init__(self) -> None:
        super().__init__(OAuth2AuthorizationRequest)

    async def get_by_state(self, state: str) -> OAuth2AuthorizationRequest | None:
        """Get authorization request by state."""
        query = self.base_stmt.where(OAuth2AuthorizationRequest.state == state)
        result = await self.execute(query)
        return result.one_or_none()

    async def get_by_uuid(self, uuid: str) -> OAuth2AuthorizationRequest | None:
        """Get authorization request by UUID."""
        query = self.base_stmt.where(OAuth2AuthorizationRequest.uuid == uuid)
        result = await self.execute(query)
        return result.one_or_none()

    async def get_by_uuid_and_app(self, uuid: str, app_id: int) -> OAuth2AuthorizationRequest | None:
        """Get authorization request by UUID and app."""
        query = self.base_stmt.where(
            and_(OAuth2AuthorizationRequest.uuid == uuid, OAuth2AuthorizationRequest.app_id == app_id)
        )
        result = await self.execute(query)
        return result.one_or_none()

    async def get_by_code(self, code: str) -> OAuth2AuthorizationRequest | None:
        """Get authorization request by code."""
        query = self.base_stmt.where(OAuth2AuthorizationRequest.code == code).options(
            selectinload(OAuth2AuthorizationRequest.account)
        )
        result = await self.execute(query)
        return result.one_or_none()

    async def mark_as_used(self, request: OAuth2AuthorizationRequest) -> OAuth2AuthorizationRequest:
        """Mark authorization request as used."""
        request.code_used = True
        await db.session.flush()
        return request

    async def update_status(
        self, request: OAuth2AuthorizationRequest, status: OAuth2RequestStatus
    ) -> OAuth2AuthorizationRequest:
        """Update request status."""
        request.status = status
        await db.session.flush()
        return request

    async def cleanup_expired(self) -> int:
        """Delete expired authorization requests."""
        query = self.base_stmt.where(OAuth2AuthorizationRequest.expires_at < datetime.utcnow())
        result = await self.execute(query)
        expired_requests = result.all()

        count = len(expired_requests)
        for request in expired_requests:
            await db.session.delete(request)

        await db.session.flush()
        return count
