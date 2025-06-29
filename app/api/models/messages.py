"""
Pydantic models for message-related API endpoints.
"""

from typing import List, Optional

from pydantic import BaseModel, Field


class EmailAddress(BaseModel):
    """Email address model."""

    name: str
    email: str


class MessageAttachment(BaseModel):
    """Message attachment model."""

    id: str
    grant_id: str
    filename: str
    size: int
    content_type: str
    is_inline: bool
    content_disposition: str


class Message(BaseModel):
    """Message model matching Nylas API structure."""

    starred: bool
    unread: bool
    folders: List[str]
    grant_id: str
    date: int
    attachments: List[MessageAttachment]
    from_: List[EmailAddress] = Field(..., alias="from")
    id: str
    object: str
    snippet: str
    subject: str
    thread_id: str
    to: List[EmailAddress]
    created_at: int
    body: str

    class Config:
        populate_by_name = True


class MessageListResponse(BaseModel):
    """Response model for listing messages."""

    request_id: str
    data: List[Message]
    next_cursor: Optional[str] = None
