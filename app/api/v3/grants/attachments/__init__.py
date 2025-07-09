"""
Attachments API router - Sub-router for attachment endpoints under grants.
"""

import logging
import uuid
from typing import Generator

from dependency_injector.wiring import Provide, inject
from fastapi import APIRouter, Depends, Path, Query, status
from fastapi.responses import JSONResponse, StreamingResponse

from app.api.middlewares.authentication import get_current_app
from app.api.models.attachments import AttachmentMetadata, AttachmentMetadataResponse
from app.api.models.error import APIError
from app.api.models.messages import MessageAttachment
from app.api.utils.errors import create_error_response, validate_grant_access
from app.container import ApplicationContainer
from app.controllers.email.email_controller import EmailController
from app.controllers.email.message import MessageResult
from app.models.app import App
from app.utils.message_utils import MessageUtils

logger = logging.getLogger(__name__)
router = APIRouter()


async def _get_attachment_from_message(
    app: App, grant_id: str, message_id: str, attachment_id: str, email_controller: EmailController
) -> tuple[MessageResult | None, MessageAttachment | None, JSONResponse | None]:
    """
    Common helper function to validate grant, get message, and find attachment.

    Returns:
        Tuple of (message_result, attachment, error_response) where error_response is None on success
    """
    # Validate grant access
    account, error_response = await validate_grant_access(app.id, grant_id)
    if error_response:
        return None, None, error_response
    assert account is not None  # account is guaranteed to be not None when error_response is None

    try:
        # Get the message by ID
        message_result = await email_controller.get_message_by_id(account, message_id)

        if message_result is None or message_result.message is None:
            error_response = create_error_response(
                error_type="not_found_error",
                message="Message not found",
                status_code=status.HTTP_404_NOT_FOUND,
                provider_error={"code": "NotFoundError", "message": "Message not found"},
            )
            return None, None, error_response

        # Find the attachment within the message
        attachment = None
        for att in message_result.message.attachments:
            if att.id == attachment_id:
                attachment = att
                break

        if attachment is None:
            error_response = create_error_response(
                error_type="not_found_error",
                message="Attachment not found",
                status_code=status.HTTP_404_NOT_FOUND,
                provider_error={"code": "NotFoundError", "message": "Attachment not found"},
            )
            return None, None, error_response

        return message_result, attachment, None

    except Exception as e:
        logger.exception(f"Failed to get attachment {attachment_id} from message {message_id}")
        error_response = create_error_response(
            error_type="provider_error",
            message="Failed to get attachment",
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            provider_error={"error": str(e)},
        )
        return None, None, error_response


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
    email_controller: EmailController = Depends(Provide[ApplicationContainer.controllers.email_controller]),
) -> AttachmentMetadataResponse | JSONResponse:
    """
    Gets metadata for a specific attachment by ID from a specific message.

    The attachment ID should be in the format returned by the messages endpoint,
    typically like "att_1", "att_2", etc. The message_id parameter is required
    to identify which message contains the attachment.
    """
    message_result, attachment, error_response = await _get_attachment_from_message(
        app, grant_id, message_id, attachment_id, email_controller
    )
    if error_response:
        return error_response
    assert (
        message_result is not None and attachment is not None
    )  # message_result and attachment are guaranteed to be not None when error_response is None

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


@router.get(
    "/{attachment_id}/download",
    response_model=None,
    responses={
        200: {
            "description": "Attachment content",
            "content": {"application/octet-stream": {"schema": {"type": "string", "format": "binary"}}},
        },
        400: {"model": APIError, "description": "Invalid grant or missing message_id"},
        404: {"model": APIError, "description": "Message or attachment not found"},
        500: {"model": APIError, "description": "Internal server error"},
    },
    summary="Download attachment content",
    description="Downloads the content of a specific attachment by ID from the specified message and grant",
)
@inject
async def download_attachment(
    grant_id: str = Path(..., example="a3ec500d-126b-4532-a632-7808721b3732"),
    attachment_id: str = Path(..., example="att_1"),
    message_id: str = Query(
        ..., example="<message-id@example.com>", description="The ID of the message containing the attachment"
    ),
    app: App = Depends(get_current_app),
    email_controller: EmailController = Depends(Provide[ApplicationContainer.controllers.email_controller]),
) -> StreamingResponse | JSONResponse:
    """
    Downloads the content of a specific attachment by ID from a specific message.

    The attachment ID should be in the format returned by the messages endpoint,
    typically like "att_1", "att_2", etc. The message_id parameter is required
    to identify which message contains the attachment.

    Returns the raw attachment content as a streaming response with appropriate
    Content-Type and Content-Disposition headers.
    """
    message_result, attachment, error_response = await _get_attachment_from_message(
        app, grant_id, message_id, attachment_id, email_controller
    )
    if error_response:
        return error_response
    assert (
        message_result is not None and attachment is not None
    )  # message_result and attachment are guaranteed to be not None when error_response is None

    try:
        # Extract attachment content from the raw message
        attachment_content = MessageUtils.extract_attachment_content(message_result.raw_message, attachment_id)

        if attachment_content is None:
            return create_error_response(
                error_type="not_found_error",
                message="Attachment content not found",
                status_code=status.HTTP_404_NOT_FOUND,
                provider_error={"code": "NotFoundError", "message": "Attachment content not found"},
            )

        # Create a generator function to stream the content
        def generate_content() -> Generator[bytes, None, None]:
            yield attachment_content

        # Set appropriate headers for file download
        headers = {
            "Content-Type": "application/octet-stream",
            "Content-Disposition": f'attachment; filename="{attachment.filename}"',
            "Content-Length": str(len(attachment_content)),
        }

        return StreamingResponse(generate_content(), media_type="application/octet-stream", headers=headers)

    except Exception:
        logger.exception(f"Failed to download attachment {attachment_id} from message {message_id}")
        return create_error_response(
            error_type="provider_error",
            message="Failed to download attachment",
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        )
