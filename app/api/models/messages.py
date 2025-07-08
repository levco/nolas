"""
Pydantic models for message-related API endpoints.
"""

from pydantic import BaseModel, Field


class EmailAddress(BaseModel):
    """Email address model."""

    name: str
    email: str


class MessageAttachment(BaseModel):
    """Message attachment model."""

    id: str
    filename: str
    size: int
    content_type: str
    is_inline: bool


class BaseMessage(BaseModel):
    """Base message model."""

    id: str
    subject: str
    body: str
    from_: list[EmailAddress] = Field(..., alias="from")
    to: list[EmailAddress] = Field(default_factory=list)
    cc: list[EmailAddress] = Field(default_factory=list)
    bcc: list[EmailAddress] = Field(default_factory=list)
    reply_to: list[EmailAddress] = Field(default_factory=list)
    reply_to_message_id: str | None = None
    attachments: list[MessageAttachment] = Field(default_factory=list)

    class Config:
        populate_by_name = True


class Message(BaseMessage):
    """Message model matching Nylas API structure."""

    starred: bool
    unread: bool
    folders: list[str]
    grant_id: str
    date: int
    object: str
    snippet: str
    thread_id: str

    class Config:
        populate_by_name = True


class MessageResponse(BaseModel):
    """Response model for getting a single message."""

    request_id: str
    data: Message


class MessageListResponse(BaseModel):
    """Response model for listing messages."""

    request_id: str
    data: list[Message]
    next_cursor: str | None = None
