"""
Pydantic models for message-related API endpoints.
"""

from typing import Any

from pydantic import BaseModel, Field, model_validator


class EmailAddress(BaseModel):
    """Email address model."""

    name: str
    email: str

    @model_validator(mode="before")
    @classmethod
    def default_name_to_email(cls, data: Any) -> Any:
        """Use the email address when a display name is not provided."""
        if isinstance(data, dict):
            name = data.get("name")
            email = data.get("email")
            if (name is None or (isinstance(name, str) and not name.strip())) and isinstance(email, str):
                return {**data, "name": email}
        return data


class MessageAttachment(BaseModel):
    """Message attachment model."""

    id: str
    filename: str
    size: int
    content_type: str
    is_inline: bool = False
    content_id: str | None = None
    content_disposition: str | None = None
    # Provider-native attachment token — excluded from API responses because it is
    # unstable (Gmail regenerates it on every message fetch).
    provider_id: str | None = Field(default=None, exclude=True)


class AttachmentData(BaseModel):
    """Attachment data model for API requests."""

    filename: str
    content_type: str
    data: bytes


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


class MessageHeader(BaseModel):
    """Single message header, returned when fields=include_headers is requested."""

    name: str
    value: str


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
    created_at: int | None = None
    headers: list[MessageHeader] | None = None

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


class UpdateMessageRequest(BaseModel):
    """Fields supported by the message update endpoint."""

    unread: bool


class SendMessageData(BaseMessage):
    """Send message model."""

    pass


class SendMessageRequest(BaseModel):
    """Request model for sending a message."""

    to: list[EmailAddress]
    subject: str
    body: str
    from_: list[EmailAddress] | None = Field(None, alias="from")
    cc: list[EmailAddress] | None = None
    bcc: list[EmailAddress] | None = None
    reply_to: list[EmailAddress] | None = None
    reply_to_message_id: str | None = None

    class Config:
        populate_by_name = True


class SendMessageResponse(BaseModel):
    """Response model for sending a message."""

    request_id: str
    grant_id: str
    data: SendMessageData
