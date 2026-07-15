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
from app.api.payloads.error import APIError
from app.api.payloads.messages import (
    AttachmentData,
    EmailAddress,
    MessageListResponse,
    MessageResponse,
    SendMessageData,
    SendMessageRequest,
    SendMessageResponse,
)
from app.api.utils.errors import (
    create_error_response,
    provider_error_response,
    validate_grant_access,
)
from app.container import ApplicationContainer
from app.controllers.providers.base import ListMessagesParams
from app.controllers.providers.exceptions import ProviderError
from app.controllers.providers.registry import ProviderRegistry
from app.controllers.smtp.smtp_controller import SMTPInvalidParameterError
from app.models import Email
from app.models.account import AccountProvider
from app.models.app import App
from app.repos.email import EmailRepo

logger = logging.getLogger(__name__)
router = APIRouter()


def _wants_headers(fields: str | None) -> bool:
    return "include_headers" in [token.strip() for token in (fields or "").split(",")]


@router.get(
    "/{message_id}",
    response_model=MessageResponse,
    response_model_exclude_none=True,
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
    registry: ProviderRegistry = Depends(Provide[ApplicationContainer.controllers.provider_registry]),
) -> MessageResponse | JSONResponse:
    """
    Gets a specific message by ID.
    """
    account, error_response = await validate_grant_access(app.id, grant_id)
    if error_response:
        return error_response
    assert account is not None  # account is guaranteed to be not None when error_response is None

    include_headers = _wants_headers(fields)
    try:
        message = await registry.get_client(account).get_message(account, message_id, include_headers=include_headers)
        if message is None:
            return create_error_response(
                error_type="not_found_error",
                message="requested object not found",
                status_code=status.HTTP_404_NOT_FOUND,
                provider_error={"code": "NotFoundError", "message": "Requested object not found"},
            )
        return MessageResponse(request_id=str(uuid.uuid4()), data=message)
    except ProviderError as e:
        return provider_error_response(e)
    except Exception:
        logger.exception(f"Failed to fetch message {message_id}")
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
    "",
    response_model=MessageListResponse,
    response_model_exclude_none=True,
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
    page_token: str | None = Query(None),
    thread_id: str | None = Query(None),
    in_: str | None = Query(None, alias="in"),
    from_: str | None = Query(None, alias="from"),
    any_email: str | None = Query(None, description="Comma-separated list of email addresses"),
    subject: str | None = Query(None),
    received_after: int | None = Query(None),
    received_before: int | None = Query(None),
    fields: str | None = Query(None),
    search_query_native: str | None = Query(None),
    query_imap: bool | None = Query(None, include_in_schema=False),
    app: App = Depends(get_current_app),
    registry: ProviderRegistry = Depends(Provide[ApplicationContainer.controllers.provider_registry]),
) -> MessageListResponse | JSONResponse:
    """
    Lists messages for a grant.
    """
    logger.info(f"Listing messages for grant {grant_id}")
    account, error_response = await validate_grant_access(app.id, grant_id)
    if error_response:
        return error_response
    assert account is not None  # account is guaranteed to be not None when error_response is None

    params = ListMessagesParams(
        limit=limit,
        page_token=page_token,
        thread_id=thread_id,
        in_=in_,
        from_=from_,
        any_email=[email.strip() for email in any_email.split(",") if email.strip()] if any_email else [],
        subject=subject,
        received_after=received_after,
        received_before=received_before,
        include_headers=_wants_headers(fields),
        search_query_native=search_query_native,
    )

    try:
        result = await registry.get_client(account).list_messages(account, params)
        return MessageListResponse(request_id=str(uuid.uuid4()), data=result.messages, next_cursor=result.next_cursor)
    except ProviderError as e:
        return provider_error_response(e)
    except Exception:
        logger.exception(f"Failed to list messages for grant {grant_id}")
        return create_error_response(
            error_type="provider_error",
            message="Failed to list messages",
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            provider_error={
                "code": "InternalError",
                "message": "An unexpected error occurred when listing messages",
            },
        )


@router.post(
    "/send",
    response_model=SendMessageResponse,
    responses={
        400: {"model": APIError, "description": "Invalid parameter or bad request"},
        422: {"model": APIError, "description": "Validation error"},
        500: {"model": APIError, "description": "Internal server error"},
    },
    summary="Send a message",
    description=(
        "Sends an email message through the specified grant's email account. Supports both JSON and multipart form "
        "data (for attachments)."
    ),
)
@inject
async def send_message(
    request: Request,
    grant_id: str = Path(..., example="a3ec500d-126b-4532-a632-7808721b3732"),
    app: App = Depends(get_current_app),
    registry: ProviderRegistry = Depends(Provide[ApplicationContainer.controllers.provider_registry]),
    email_repo: EmailRepo = Depends(Provide[ApplicationContainer.repos.email]),
) -> SendMessageResponse | JSONResponse:
    """
    Sends the specified message.
    """
    logger.info(f"Sending message for grant {grant_id}")
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

        sender = message_data.from_ or [EmailAddress(name=account.email, email=account.email)]
        result = await registry.get_client(account).send_message(
            account=account,
            to=message_data.to,
            subject=message_data.subject,
            body=message_data.body,
            from_=sender,
            cc=message_data.cc,
            bcc=message_data.bcc,
            reply_to=message_data.reply_to,
            reply_to_message_id=message_data.reply_to_message_id,
            attachments=attachments,
        )

        # Record API-sent messages so incoming notifications don't echo them back.
        if account.provider in (AccountProvider.google, AccountProvider.microsoft):
            existing = await email_repo.get_by_account_and_email_id(account.id, result.message_id)
            if existing is None:
                await email_repo.add(
                    Email(
                        account_id=account.id,
                        email_id=result.message_id,
                        thread_id=result.thread_id,
                        folder="SENT",
                    )
                )

        response_data = SendMessageData(
            id=result.message_id,
            subject=message_data.subject,
            body=message_data.body,
            from_=sender,
            to=message_data.to,
            cc=message_data.cc or [],
            bcc=message_data.bcc or [],
            reply_to=message_data.reply_to or [],
            reply_to_message_id=message_data.reply_to_message_id,
        )
        return SendMessageResponse(request_id=str(uuid.uuid4()), grant_id=grant_id, data=response_data)

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
    except ProviderError as e:
        return provider_error_response(e)
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

    form = await request.form()
    for field_name, field_value in form.multi_items():
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
