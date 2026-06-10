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
from app.api.payloads.attachments import AttachmentMetadata, AttachmentMetadataResponse
from app.api.payloads.error import APIError
from app.api.utils.errors import create_error_response, provider_error_response, validate_grant_access
from app.container import ApplicationContainer
from app.controllers.providers.exceptions import ProviderError
from app.controllers.providers.registry import ProviderRegistry
from app.models.app import App

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
    registry: ProviderRegistry = Depends(Provide[ApplicationContainer.controllers.provider_registry]),
) -> AttachmentMetadataResponse | JSONResponse:
    """
    Gets metadata for a specific attachment by ID from a specific message.
    """
    account, error_response = await validate_grant_access(app.id, grant_id)
    if error_response:
        return error_response
    assert account is not None  # account is guaranteed to be not None when error_response is None

    try:
        attachment = await registry.get_client(account).get_attachment_metadata(account, message_id, attachment_id)
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
                is_inline=attachment.is_inline,
                content_id=attachment.content_id,
                content_disposition=attachment.content_disposition,
            ),
        )
    except ProviderError as e:
        return provider_error_response(e)
    except Exception:
        logger.exception(f"Failed to get attachment {attachment_id} from message {message_id}")
        return create_error_response(
            error_type="provider_error",
            message="Failed to get attachment",
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            provider_error={"code": "InternalServerError", "message": "Failed to get attachment"},
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
    registry: ProviderRegistry = Depends(Provide[ApplicationContainer.controllers.provider_registry]),
) -> StreamingResponse | JSONResponse:
    """
    Downloads the content of a specific attachment by ID from a specific message.

    Returns the raw attachment content as a streaming response with appropriate
    Content-Type and Content-Disposition headers.
    """
    account, error_response = await validate_grant_access(app.id, grant_id)
    if error_response:
        return error_response
    assert account is not None  # account is guaranteed to be not None when error_response is None

    try:
        content = await registry.get_client(account).download_attachment(account, message_id, attachment_id)
        if content is None:
            return create_error_response(
                error_type="not_found_error",
                message="Attachment content not found",
                status_code=status.HTTP_404_NOT_FOUND,
                provider_error={"code": "NotFoundError", "message": "Attachment content not found"},
            )

        attachment_content = content.data

        def generate_content() -> Generator[bytes, None, None]:
            yield attachment_content

        headers = {
            "Content-Type": "application/octet-stream",
            "Content-Disposition": f'attachment; filename="{content.filename}"',
            "Content-Length": str(len(attachment_content)),
        }
        return StreamingResponse(generate_content(), media_type="application/octet-stream", headers=headers)

    except ProviderError as e:
        return provider_error_response(e)
    except Exception:
        logger.exception(f"Failed to download attachment {attachment_id} from message {message_id}")
        return create_error_response(
            error_type="provider_error",
            message="Failed to download attachment",
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        )
