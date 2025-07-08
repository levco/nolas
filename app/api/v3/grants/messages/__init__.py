"""
Messages API router - Sub-router for message endpoints under grants.
"""

import logging
import uuid
from typing import Any

from dependency_injector.wiring import Provide, inject
from fastapi import APIRouter, Depends, Path, Query, status
from fastapi.responses import JSONResponse

from app.api.middlewares.authentication import get_current_app
from app.api.models import MessageListResponse, MessageResponse
from app.api.models.error import ErrorDetail
from app.api.models.send_messages import (
    SendMessageError,
    SendMessageRequest,
    SendMessageResponse,
)
from app.container import ApplicationContainer
from app.controllers.email.email_controller import EmailController
from app.controllers.imap.message_controller import MessageController
from app.controllers.smtp.smtp_controller import SMTPInvalidParameterError
from app.models.app import App
from app.repos.account import AccountRepo

logger = logging.getLogger(__name__)
router = APIRouter()


def create_error_response(
    error_type: str, message: str, status_code: int, provider_error: dict[str, Any] | None = None
) -> JSONResponse:
    """
    Create a structured error response that matches the Nylas API schema.

    Args:
        error_type: The type of error (e.g., "invalid_request_error", "api.internal_error")
        message: Human-readable error message
        status_code: HTTP status code
        provider_error: Optional provider-specific error details

    Returns:
        JSONResponse with structured error format
    """
    error_response = SendMessageError(
        request_id=str(uuid.uuid4()), error=ErrorDetail(type=error_type, message=message, provider_error=provider_error)
    )
    return JSONResponse(status_code=status_code, content=error_response.model_dump())


@router.get(
    "/{message_id}",
    response_model=MessageResponse,
    responses={
        400: {"model": SendMessageError, "description": "Invalid grant"},
        404: {"model": SendMessageError, "description": "Message not found"},
        500: {"model": SendMessageError, "description": "Internal server error"},
    },
    summary="Get a specific message",
    description="Gets a specific message by ID for the specified grant",
)
@inject
async def get_message(
    grant_id: str = Path(..., example="a3ec500d-126b-4532-a632-7808721b3732"),
    message_id: str = Path(..., example="1234567890"),
    app: App = Depends(get_current_app),
    account_repo: AccountRepo = Depends(Provide[ApplicationContainer.repos.account]),
    email_controller: EmailController = Depends(Provide[ApplicationContainer.controllers.email_controller]),
) -> MessageResponse | JSONResponse:
    """
    Gets a specific message by ID.
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
        # Try to fetch the actual message from IMAP
        message_result = await email_controller.get_message_by_id(account, message_id)

        if message_result is None:
            return create_error_response(
                error_type="not_found_error",
                message="Message not found",
                status_code=status.HTTP_404_NOT_FOUND,
                provider_error={"message_id": message_id},
            )

        return MessageResponse(request_id=str(uuid.uuid4()), data=message_result.message)

    except Exception as e:
        logger.exception(f"Failed to fetch message {message_id} from IMAP")
        return create_error_response(
            error_type="provider_error",
            message="Failed to fetch message",
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            provider_error={"error": str(e)},
        )


@router.get(
    "/",
    response_model=MessageListResponse,
    responses={
        400: {"model": SendMessageError, "description": "Invalid parameter or bad request"},
        500: {"model": SendMessageError, "description": "Internal server error"},
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
    account_repo: AccountRepo = Depends(Provide[ApplicationContainer.repos.account]),
    message_controller: MessageController = Depends(Provide[ApplicationContainer.controllers.imap_message_controller]),
) -> MessageListResponse | JSONResponse:
    """
    Lists messages for a grant.
    """
    account = await account_repo.get_by_app_and_uuid(app.id, grant_id)
    if account is None:
        return create_error_response(
            error_type="invalid_request_error", message="Invalid grant", status_code=status.HTTP_400_BAD_REQUEST
        )

    return create_error_response(
        error_type="unsupported_operation_error", message="Not implemented", status_code=status.HTTP_501_NOT_IMPLEMENTED
    )


@router.post(
    "/send",
    response_model=SendMessageResponse,
    responses={
        400: {"model": SendMessageError, "description": "Invalid parameter or bad request"},
        422: {"model": SendMessageError, "description": "Validation error"},
        500: {"model": SendMessageError, "description": "Internal server error"},
    },
    summary="Send a message",
    description="Sends an email message through the specified grant's email account",
)
@inject
async def send_message(
    request: SendMessageRequest,
    grant_id: str = Path(..., example="a3ec500d-126b-4532-a632-7808721b3732"),
    app: App = Depends(get_current_app),
    account_repo: AccountRepo = Depends(Provide[ApplicationContainer.repos.account]),
    email_controller: EmailController = Depends(Provide[ApplicationContainer.controllers.email_controller]),
) -> SendMessageResponse | JSONResponse:
    """
    Sends the specified message.
    """
    account = await account_repo.get_by_app_and_uuid(app.id, grant_id)
    if account is None:
        return create_error_response(
            error_type="invalid_request_error", message="Invalid grant", status_code=status.HTTP_400_BAD_REQUEST
        )

    try:
        send_message_result = await email_controller.send_email(
            account=account,
            to=request.to,
            subject=request.subject,
            body=request.body,
            from_=request.from_,
            cc=request.cc,
            bcc=request.bcc,
            reply_to=request.reply_to,
            reply_to_message_id=request.reply_to_message_id,
        )
        return SendMessageResponse(request_id=str(uuid.uuid4()), grant_id=grant_id, data=send_message_result.message)

    except SMTPInvalidParameterError as e:
        return create_error_response(
            error_type="invalid_request_error",
            message=f"Invalid parameter: {e.parameter}",
            status_code=status.HTTP_400_BAD_REQUEST,
            provider_error={"parameter": e.parameter, "value": str(e.value)},
        )
    except Exception as e:
        logger.exception("Failed to send message")
        return create_error_response(
            error_type="provider_error",
            message="An unexpected error occurred when sending the message",
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            provider_error={"error": str(e)},
        )
