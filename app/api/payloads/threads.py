"""
Pydantic models for thread-related API endpoints.
"""

from pydantic import BaseModel, Field

from app.api.payloads.messages import EmailAddress, Message


class Thread(BaseModel):
    """Thread model similar to Nylas API structure."""

    id: str
    object: str = "thread"
    grant_id: str
    subject: str
    snippet: str
    participants: list[EmailAddress] = Field(default_factory=list)
    message_ids: list[str] = Field(default_factory=list)
    latest_draft_or_message: Message
    has_attachments: bool = False
    starred: bool = False
    unread: bool = False
    latest_message_received_date: int
    earliest_message_date: int


class ThreadListResponse(BaseModel):
    """Response model for listing threads."""

    request_id: str
    data: list[Thread]
    next_cursor: str | None = None
