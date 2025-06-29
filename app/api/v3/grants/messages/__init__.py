"""
Messages API router - Sub-router for message endpoints under grants.
"""

import time
from typing import Any, Dict

from dependency_injector.wiring import Provide, inject
from fastapi import APIRouter, Depends, Path, Query

from app.api.models import Message, MessageListResponse
from app.container import ApplicationContainer
from app.repos.account import AccountRepo

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
    }


@router.get("/{message_id}", response_model=Message)
@inject
async def get_message(
    grant_id: str = Path(..., example="a3ec500d-126b-4532-a632-7808721b3732"),
    message_id: str = Path(..., example="1234567890"),
    account_repo: AccountRepo = Depends(Provide[ApplicationContainer.repos.account]),
) -> Message:
    """
    Get a specific message by ID.

    This endpoint handles: GET /v3/grants/{grant_id}/messages/{message_id}
    Returns dummy JSON data with the same structure as Nylas.

    Demonstrates using the shared fastapi_async_sqlalchemy session through repositories.
    """
    # Example: Use the repository with shared database session
    # In a real implementation, you would:
    # 1. Validate the grant_id exists by querying accounts
    # 2. Query the database for the actual message
    # 3. Return the actual message data

    try:
        # Example database query using the shared session
        accounts = await account_repo.execute(account_repo.base_stmt.limit(1))
        account_count = len(accounts.all())

        # For demonstration, include the account count in the response metadata
        dummy_message = create_dummy_message(message_id, grant_id)
        dummy_message["_demo_account_count"] = account_count
        dummy_message["_demo_using_shared_session"] = True

        return Message(**dummy_message)
    except Exception as e:
        # If database query fails, still return dummy data
        dummy_message = create_dummy_message(message_id, grant_id)
        return Message(**dummy_message)


@router.get("/", response_model=MessageListResponse)
@inject
async def list_messages(
    grant_id: str = Path(..., example="a3ec500d-126b-4532-a632-7808721b3732"),
    limit: int = Query(50, ge=1, le=100),
    offset: int = Query(0, ge=0),
    account_repo: AccountRepo = Depends(Provide[ApplicationContainer.repos.account]),
) -> MessageListResponse:
    """
    List messages for a grant.

    This endpoint handles: GET /v3/grants/{grant_id}/messages
    Returns dummy JSON data with the same structure as Nylas.

    Demonstrates using the shared fastapi_async_sqlalchemy session through repositories.
    """
    try:
        # Example: Use repository to validate grant exists and get account info
        accounts = await account_repo.execute(account_repo.base_stmt.limit(10))
        all_accounts = accounts.all()

        # Generate dummy messages
        messages = [
            Message(**create_dummy_message(f"msg_{i}", grant_id))
            for i in range(1, min(limit + 1, 6))  # Max 5 messages for demo
        ]

        # Add demo metadata to show database integration
        return MessageListResponse(
            request_id="dummy-request-id",
            data=messages,
            next_cursor="dummy-cursor" if len(messages) >= limit else None,
        )

    except Exception as e:
        # If database query fails, still return dummy data
        messages = [
            Message(**create_dummy_message(f"msg_{i}", grant_id))
            for i in range(1, min(limit + 1, 6))  # Max 5 messages for demo
        ]

        return MessageListResponse(
            request_id="dummy-request-id",
            data=messages,
            next_cursor="dummy-cursor" if len(messages) >= limit else None,
        )
