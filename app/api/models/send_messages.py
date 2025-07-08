"""
Pydantic models for send message API endpoints.
"""

from pydantic import BaseModel, Field

from .error import ErrorDetail
from .messages import BaseMessage, EmailAddress


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


class SendMessageError(BaseModel):
    """Error response model for sending a message that matches Nylas API schema."""

    request_id: str
    error: ErrorDetail
