"""
Pydantic models for grant-related API endpoints.
"""

import uuid

from pydantic import BaseModel, Field


class DeleteGrantResponse(BaseModel):
    """Response model for deleting a grant."""

    request_id: str = Field(..., description="Unique request identifier")
    success: bool = Field(True, description="Whether the deletion was successful")


class GrantData(BaseModel):
    """Grant model matching the Nylas v3 grants API."""

    id: str
    provider: str
    email: str
    grant_status: str
    scope: list[str] = Field(default_factory=list)
    created_at: int | None = None
    updated_at: int | None = None


class GrantResponse(BaseModel):
    """Response model for getting or updating a grant."""

    request_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    data: GrantData


class GrantSettings(BaseModel):
    refresh_token: str | None = None


class UpdateGrantRequest(BaseModel):
    """PATCH /v3/grants/{id} body."""

    settings: GrantSettings = Field(default_factory=GrantSettings)


class ConnectCustomRequest(BaseModel):
    """POST /v3/connect/custom body (Nylas custom auth)."""

    provider: str
    settings: GrantSettings = Field(default_factory=GrantSettings)


class ProviderDetectData(BaseModel):
    email_address: str
    provider: str | None = None
    detected: bool = False


class ProviderDetectResponse(BaseModel):
    request_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    data: ProviderDetectData
