"""
API models package for Pydantic response/request models.
"""

from .messages import (
    EmailAddress,
    Message,
    MessageAttachment,
    MessageListResponse,
    MessageResponse,
    SendMessageRequest,
    SendMessageResponse,
)

__all__ = [
    "EmailAddress",
    "Message",
    "MessageAttachment",
    "MessageListResponse",
    "MessageResponse",
    "SendMessageRequest",
    "SendMessageResponse",
]
