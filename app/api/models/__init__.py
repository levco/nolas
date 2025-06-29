"""
API models package for Pydantic response/request models.
"""

from .messages import (
    EmailAddress,
    Message,
    MessageAttachment,
    MessageListResponse,
    MessageResponse,
)

__all__ = [
    "EmailAddress",
    "Message",
    "MessageAttachment",
    "MessageListResponse",
    "MessageResponse",
]
