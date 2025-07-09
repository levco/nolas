"""
Attachments API router - Sub-router for attachment endpoints under grants.
"""

import logging
import uuid

from dependency_injector.wiring import Provide, inject
from fastapi import APIRouter, Depends, Path, Query, status
from fastapi.responses import JSONResponse

from app.api.middlewares.authentication import get_current_app
from app.api.models.attachments import AttachmentMetadata, AttachmentMetadataResponse
from app.api.models.error import APIError
from app.api.utils.errors import create_error_response
from app.container import ApplicationContainer
from app.controllers.email.email_controller import EmailController
from app.models.app import App
from app.repos.account import AccountRepo

logger = logging.getLogger(__name__)
router = APIRouter()


@router.get(
    "/{attachment_id}",
    response_model=AttachmentMetadataResponse,
    responses={
        400: {"model": APIError, "description": "Invalid grant or missing message_id"},
        404: {"model": APIError, "description": "Message or attachment not found"},
        500: {"model": APIError, "description": "Internal server error"},
    },
    summary="Get attachment metadata",
    description="Gets metadata for a specific attachment by ID from the specified message and grant",
)
@inject
async def get_attachment(
    grant_id: str = Path(..., example="a3ec500d-126b-4532-a632-7808721b3732"),
    attachment_id: str = Path(..., example="att_1"),
    message_id: str = Query(
        ..., example="<message-id@example.com>", description="The ID of the message containing the attachment"
    ),
    app: App = Depends(get_current_app),
    account_repo: AccountRepo = Depends(Provide[ApplicationContainer.repos.account]),
    email_controller: EmailController = Depends(Provide[ApplicationContainer.controllers.email_controller]),
) -> AttachmentMetadataResponse | JSONResponse:
    """
    Gets metadata for a specific attachment by ID from a specific message.

    The attachment ID should be in the format returned by the messages endpoint,
    typically like "att_1", "att_2", etc. The message_id parameter is required
    to identify which message contains the attachment.
    """
    account = await account_repo.get_by_app_and_uuid(app.id, grant_id)
    if account is None:
        return create_error_response(
            error_type="invalid_request_error",
            message="Invalid grant",
            status_code=status.HTTP_400_BAD_REQUEST,
            provider_error={"grant_id": grant_id},
        )

    try:
        # First get the message by ID
        message_result = await email_controller.get_message_by_id(account, message_id)

        if message_result is None or message_result.message is None:
            return create_error_response(
                error_type="not_found_error",
                message="Message not found",
                status_code=status.HTTP_404_NOT_FOUND,
                provider_error={"code": "NotFoundError", "message": "Message not found"},
            )

        # Find the attachment within the message
        attachment = None
        for att in message_result.message.attachments:
            if att.id == attachment_id:
                attachment = att
                break

        if attachment is None:
            return create_error_response(
                error_type="not_found_error",
                message="Attachment not found",
                status_code=status.HTTP_404_NOT_FOUND,
                provider_error={"code": "NotFoundError", "message": "Attachment not found"},
            )

        return AttachmentMetadataResponse(
            request_id=str(uuid.uuid4()),
            data=AttachmentMetadata(
                id=attachment.id,
                content_type=attachment.content_type,
                filename=attachment.filename,
                size=attachment.size,
                grant_id=grant_id,
            ),
        )

    except Exception as e:
        logger.exception(f"Failed to fetch attachment {attachment_id} from message {message_id}")
        return create_error_response(
            error_type="provider_error",
            message="Failed to fetch attachment",
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            provider_error={"error": str(e)},
        )
