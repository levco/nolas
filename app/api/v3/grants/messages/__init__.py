"""
Messages API router - Sub-router for message endpoints under grants.
"""

import json
import logging
import mimetypes
import uuid
from typing import Any

from dependency_injector.wiring import Provide, inject
from fastapi import APIRouter, Depends, Path, Query, Request, status
from fastapi.responses import JSONResponse

from app.api.middlewares.authentication import get_current_app
from app.api.payloads import (
    MessageListResponse,
    MessageResponse,
    SendMessageRequest,
    SendMessageResponse,
)
from app.api.payloads.error import APIError
from app.api.payloads.messages import AttachmentData
from app.api.utils.errors import create_error_response, validate_grant_access
from app.container import ApplicationContainer
from app.controllers.email.email_controller import EmailController
from app.controllers.imap.message_controller import MessageController
from app.controllers.smtp.smtp_controller import SMTPInvalidParameterError
from app.models.app import App

logger = logging.getLogger(__name__)
router = APIRouter()


@router.get(
    "/{message_id}",
    response_model=MessageResponse,
    responses={
        400: {"model": APIError, "description": "Invalid grant"},
        404: {"model": APIError, "description": "Message not found"},
        500: {"model": APIError, "description": "Internal server error"},
    },
    summary="Get a specific message",
    description="Gets a specific message by ID for the specified grant",
)
@inject
async def get_message(
    grant_id: str = Path(..., example="a3ec500d-126b-4532-a632-7808721b3732"),
    message_id: str = Path(..., example="1234567890"),
    fields: str = Query(None, description="Comma-separated list of fields to include"),
    app: App = Depends(get_current_app),
    email_controller: EmailController = Depends(Provide[ApplicationContainer.controllers.email_controller]),
) -> MessageResponse | JSONResponse:
    """
    Gets a specific message by ID.
    """
    account, error_response = await validate_grant_access(app.id, grant_id)
    if error_response:
        return error_response
    assert account is not None  # account is guaranteed to be not None when error_response is None

    try:
        # Try to fetch the actual message from IMAP
        message_result = await email_controller.get_message_by_id(account, message_id)

        if message_result is None:
            return create_error_response(
                error_type="not_found_error",
                message="requested object not found",
                status_code=status.HTTP_404_NOT_FOUND,
                provider_error={"code": "NotFoundError", "message": "Requested object not found"},
            )

        return MessageResponse(request_id=str(uuid.uuid4()), data=message_result.message)
    except Exception:
        logger.exception(f"Failed to fetch message {message_id} from IMAP")
        return create_error_response(
            error_type="provider_error",
            message="Failed to fetch message",
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            provider_error={
                "code": "InternalError",
                "message": "An unexpected error occurred when fetching the message",
            },
        )


@router.get(
    "/",
    response_model=MessageListResponse,
    responses={
        400: {"model": APIError, "description": "Invalid parameter or bad request"},
        500: {"model": APIError, "description": "Internal server error"},
    },
    summary="List messages",
    description="Lists messages for the specified grant",
)
@inject
async def list_messages(
    grant_id: str = Path(..., example="a3ec500d-126b-4532-a632-7808721b3732"),
    limit: int = Query(50, ge=1, le=100),
    offset: int = Query(0, ge=0),
    app: App = Depends(get_current_app),
    message_controller: MessageController = Depends(Provide[ApplicationContainer.controllers.imap_message_controller]),
) -> MessageListResponse | JSONResponse:
    """
    Lists messages for a grant.
    """
    account, error_response = await validate_grant_access(app.id, grant_id)
    if error_response:
        return error_response
    assert account is not None  # account is guaranteed to be not None when error_response is None

    # TODO: Implement message listing
    return MessageListResponse(request_id=str(uuid.uuid4()), data=[], next_cursor=None)


@router.post(
    "/send",
    response_model=SendMessageResponse,
    responses={
        400: {"model": APIError, "description": "Invalid parameter or bad request"},
        422: {"model": APIError, "description": "Validation error"},
        500: {"model": APIError, "description": "Internal server error"},
    },
    summary="Send a message",
    description="Sends an email message through the specified grant's email account. Supports both JSON and multipart form data (for attachments).",
)
@inject
async def send_message(
    request: Request,
    grant_id: str = Path(..., example="a3ec500d-126b-4532-a632-7808721b3732"),
    app: App = Depends(get_current_app),
    email_controller: EmailController = Depends(Provide[ApplicationContainer.controllers.email_controller]),
) -> SendMessageResponse | JSONResponse:
    """
    Sends the specified message.
    """
    account, error_response = await validate_grant_access(app.id, grant_id)
    if error_response:
        return error_response
    assert account is not None  # account is guaranteed to be not None when error_response is None

    try:
        # Check if this is a multipart request (with attachments)
        content_type = request.headers.get("content-type", "")
        if content_type.startswith("multipart/form-data"):
            message_data, attachments = await _parse_multipart_request(request)
        else:
            body = await request.body()
            message_data = SendMessageRequest.model_validate_json(body)
            attachments = []

        send_message_result = await email_controller.send_email(
            account=account,
            to=message_data.to,
            subject=message_data.subject,
            body=message_data.body,
            from_=message_data.from_,
            cc=message_data.cc,
            bcc=message_data.bcc,
            reply_to=message_data.reply_to,
            reply_to_message_id=message_data.reply_to_message_id,
            attachments=attachments,
        )
        return SendMessageResponse(request_id=str(uuid.uuid4()), grant_id=grant_id, data=send_message_result.message)

    except SMTPInvalidParameterError as e:
        return create_error_response(
            error_type="invalid_request_error",
            message=f"Invalid parameter: {e.parameter}",
            status_code=status.HTTP_400_BAD_REQUEST,
            provider_error={
                "code": "InvalidParameterError",
                "message": f"Invalid parameter: {e.parameter}",
            },
        )
    except Exception:
        logger.exception("Failed to send message")
        return create_error_response(
            error_type="provider_error",
            message="An unexpected error occurred when sending the message",
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            provider_error={
                "code": "InternalError",
                "message": "An unexpected error occurred when sending the message",
            },
        )


async def _parse_multipart_request(request: Request) -> tuple[SendMessageRequest, list[AttachmentData]]:
    """
    Parse multipart form data request to extract message and attachments.

    Expected format:
    - "Message" field: JSON string with message data
    - "Attachment" fields: File uploads
    """
    attachments: list[AttachmentData] = []
    message_json: dict[str, Any] | None = None

    # Parse multipart form data
    form = await request.form()

    for field_name, field_value in form.items():
        if field_name == "Message":
            # Parse the JSON message data
            if isinstance(field_value, str):
                message_json = json.loads(field_value)
            else:
                raise ValueError("Message field must be a JSON string")
        elif field_name == "Attachment":
            # Handle attachment
            if hasattr(field_value, "filename") and hasattr(field_value, "read"):
                # It's an UploadFile
                file_content = await field_value.read()
                filename = field_value.filename or "unknown"

                # Determine content type based on filename extension
                content_type, _ = mimetypes.guess_type(filename)
                if content_type is None:
                    content_type = "application/octet-stream"

                attachment = AttachmentData(filename=filename, content_type=content_type, data=file_content)
                attachments.append(attachment)

    if message_json is None:
        raise ValueError("Missing 'Message' field in multipart request")

    # Parse the message data
    message_data = SendMessageRequest.model_validate(message_json)

    return message_data, attachments
