"""
API models package for Pydantic response/request models.
"""

from .messages import (
    AttachmentData,
    EmailAddress,
    Message,
    MessageAttachment,
    MessageListResponse,
    MessageResponse,
    SendMessageRequest,
    SendMessageResponse,
)

__all__ = [
    "AttachmentData",
    "AttachmentResponse",
    "EmailAddress",
    "Message",
    "MessageAttachment",
    "MessageListResponse",
    "MessageResponse",
    "SendMessageRequest",
    "SendMessageResponse",
]
