"""
Messages API router - Sub-router for message endpoints under grants.
"""

import logging
import time
import uuid
from typing import Any, Dict

from dependency_injector.wiring import Provide, inject
from fastapi import APIRouter, Depends, HTTPException, Path, Query, status

from app.api.middlewares.authentication import get_current_app
from app.api.models import Message, MessageListResponse, MessageResponse
from app.container import ApplicationContainer
from app.controllers.imap.message_controller import MessageController
from app.models.app import App
from app.repos.account import AccountRepo

logger = logging.getLogger(__name__)
router = APIRouter()


def create_dummy_message(message_id: str, grant_id: str) -> Dict[str, Any]:
    """Create dummy message data that matches Nylas API structure."""
    current_timestamp = int(time.time())

    return {
        "starred": False,
        "unread": True,
        "folders": ["UNREAD", "CATEGORY_PERSONAL", "INBOX"],
        "grant_id": grant_id,
        "date": current_timestamp,
        "attachments": [
            {
                "id": "att_1",
                "grant_id": grant_id,
                "filename": "example.pdf",
                "size": 1024,
                "content_type": "application/pdf",
                "is_inline": False,
                "content_disposition": 'attachment; filename="example.pdf"',
            }
        ],
        "from": [{"name": "John Doe", "email": "john@example.com"}],
        "id": message_id,
        "object": "message",
        "snippet": "This is a sample email message from the Nolas API",
        "subject": "Sample Email Subject",
        "thread_id": f"thread_{message_id}",
        "to": [{"name": "Jane Smith", "email": "jane@example.com"}],
        "created_at": current_timestamp,
        "body": "This is the body of a sample email message. It contains the main content of the email.",
        "references": [],
    }


@router.get("/{message_id}", response_model=MessageResponse)
@inject
async def get_message(
    grant_id: str = Path(..., example="a3ec500d-126b-4532-a632-7808721b3732"),
    message_id: str = Path(..., example="1234567890"),
    app: App = Depends(get_current_app),
    account_repo: AccountRepo = Depends(Provide[ApplicationContainer.repos.account]),
    message_controller: MessageController = Depends(Provide[ApplicationContainer.controllers.imap_message_controller]),
) -> MessageResponse:
    """
    Get a specific message by ID.

    This endpoint handles: GET /v3/grants/{grant_id}/messages/{message_id}

    Fetches actual email content from IMAP server using Message-ID search.
    """
    account = await account_repo.get_by_app_and_uuid(app.id, grant_id)
    if account is None:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid grant")

    try:
        # Try to fetch the actual message from IMAP
        message = await message_controller.get_message_by_id(account, message_id)

        if message is None:
            # If not found by Message-ID, fall back to dummy data for now
            # In production, you might want to return 404 instead
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Message not found")

        return MessageResponse(request_id=str(uuid.uuid4()), data=message)

    except Exception:
        logger.exception(f"Failed to fetch message {message_id} from IMAP")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Failed to fetch message")


@router.get("/", response_model=MessageListResponse)
@inject
async def list_messages(
    grant_id: str = Path(..., example="a3ec500d-126b-4532-a632-7808721b3732"),
    limit: int = Query(50, ge=1, le=100),
    offset: int = Query(0, ge=0),
    app: App = Depends(get_current_app),
    account_repo: AccountRepo = Depends(Provide[ApplicationContainer.repos.account]),
    message_controller: MessageController = Depends(Provide[ApplicationContainer.controllers.imap_message_controller]),
) -> MessageListResponse:
    """
    List messages for a grant.

    This endpoint handles: GET /v3/grants/{grant_id}/messages
    Fetches actual messages from IMAP server with fallback to dummy data.
    """
    account = await account_repo.get_by_app_and_uuid(app.id, grant_id)
    if account is None:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid grant")

    try:
        # Try to fetch actual messages from IMAP
        messages = await message_controller.list_messages(account, folder="INBOX", limit=limit, offset=offset)

        if messages:
            return MessageListResponse(
                request_id="imap-request-id",
                data=messages,
                next_cursor="imap-cursor" if len(messages) >= limit else None,
            )
        else:
            # Fall back to dummy data if no messages found
            dummy_messages = [
                Message(**create_dummy_message(f"msg_{i}", grant_id))
                for i in range(1, min(limit + 1, 6))  # Max 5 messages for demo
            ]
            return MessageListResponse(
                request_id="dummy-request-id",
                data=dummy_messages,
                next_cursor="dummy-cursor" if len(dummy_messages) >= limit else None,
            )

    except Exception as e:
        # Log the error and fall back to dummy data
        import logging

        logger = logging.getLogger(__name__)
        logger.error(f"Failed to fetch messages from IMAP: {e}")

        # If IMAP fails, return dummy data
        dummy_messages = [
            Message(**create_dummy_message(f"msg_{i}", grant_id))
            for i in range(1, min(limit + 1, 6))  # Max 5 messages for demo
        ]

        return MessageListResponse(
            request_id="dummy-request-id",
            data=dummy_messages,
            next_cursor="dummy-cursor" if len(dummy_messages) >= limit else None,
        )
