"""
Pydantic models for grant-related API endpoints.
"""

from pydantic import BaseModel, Field


class DeleteGrantResponse(BaseModel):
    """Response model for deleting a grant."""

    request_id: str = Field(..., description="Unique request identifier")
    success: bool = Field(True, description="Whether the deletion was successful")
