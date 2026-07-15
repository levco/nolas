"""
Threads API router - Sub-router for thread endpoints under grants.
"""

import logging
import uuid

from dependency_injector.wiring import Provide, inject
from fastapi import APIRouter, Depends, Path, Query, status
from fastapi.responses import JSONResponse

from app.api.middlewares.authentication import get_current_app
from app.api.payloads.error import APIError
from app.api.payloads.threads import ThreadListResponse, ThreadResponse
from app.api.utils.errors import create_error_response, provider_error_response, validate_grant_access
from app.container import ApplicationContainer
from app.controllers.providers.base import ListThreadsParams
from app.controllers.providers.exceptions import ProviderError
from app.controllers.providers.registry import ProviderRegistry
from app.models.app import App

logger = logging.getLogger(__name__)
router = APIRouter()


@router.get(
    "/{thread_id}",
    response_model=ThreadResponse,
    response_model_exclude_none=True,
    responses={
        400: {"model": APIError, "description": "Invalid grant"},
        404: {"model": APIError, "description": "Thread not found"},
        500: {"model": APIError, "description": "Internal server error"},
    },
    summary="Get a specific thread",
    description="Gets a specific thread by ID for the specified grant",
)
@inject
async def get_thread(
    grant_id: str = Path(..., example="a3ec500d-126b-4532-a632-7808721b3732"),
    thread_id: str = Path(..., example="19f376d9516d4c5a"),
    app: App = Depends(get_current_app),
    registry: ProviderRegistry = Depends(Provide[ApplicationContainer.controllers.provider_registry]),
) -> ThreadResponse | JSONResponse:
    logger.info(f"Fetching thread {thread_id} for grant {grant_id}")
    account, error_response = await validate_grant_access(app.id, grant_id)
    if error_response:
        return error_response
    assert account is not None  # account is guaranteed to be not None when error_response is None

    try:
        thread = await registry.get_client(account).get_thread(account, thread_id)
        if thread is None:
            return create_error_response(
                error_type="not_found_error",
                message="requested object not found",
                status_code=status.HTTP_404_NOT_FOUND,
                provider_error={"code": "NotFoundError", "message": "Requested object not found"},
            )
        return ThreadResponse(request_id=str(uuid.uuid4()), data=thread)
    except ProviderError as e:
        return provider_error_response(e)
    except Exception:
        logger.exception(f"Failed to fetch thread {thread_id}")
        return create_error_response(
            error_type="provider_error",
            message="Failed to fetch thread",
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            provider_error={
                "code": "InternalError",
                "message": "An unexpected error occurred when fetching the thread",
            },
        )


@router.get(
    "",
    response_model=ThreadListResponse,
    response_model_exclude_none=True,
    responses={
        400: {"model": APIError, "description": "Invalid parameter or bad request"},
        500: {"model": APIError, "description": "Internal server error"},
    },
    summary="List threads",
    description="Lists threads for the specified grant",
)
@inject
async def list_threads(
    grant_id: str = Path(..., example="a3ec500d-126b-4532-a632-7808721b3732"),
    limit: int = Query(20, ge=1, le=100),
    page_token: str | None = Query(None),
    in_: str | None = Query(None, alias="in"),
    from_: str | None = Query(None, alias="from"),
    to: str | None = Query(None),
    cc: str | None = Query(None),
    bcc: str | None = Query(None),
    any_email: str | None = Query(None, description="Comma-separated list of email addresses"),
    subject: str | None = Query(None),
    latest_message_after: int | None = Query(None),
    latest_message_before: int | None = Query(None),
    unread: bool | None = Query(None),
    starred: bool | None = Query(None),
    has_attachment: bool | None = Query(None),
    search_query_native: str | None = Query(None),
    app: App = Depends(get_current_app),
    registry: ProviderRegistry = Depends(Provide[ApplicationContainer.controllers.provider_registry]),
) -> ThreadListResponse | JSONResponse:
    logger.info(f"Listing threads for grant {grant_id}")
    account, error_response = await validate_grant_access(app.id, grant_id)
    if error_response:
        return error_response
    assert account is not None  # account is guaranteed to be not None when error_response is None

    parsed_any_email = [email.strip() for email in any_email.split(",") if email.strip()] if any_email else []
    if len(parsed_any_email) > 25:
        return create_error_response(
            error_type="invalid_request_error",
            message="A maximum of 25 any_email values are allowed.",
            status_code=status.HTTP_400_BAD_REQUEST,
            provider_error={
                "code": "InvalidParameterError",
                "message": "A maximum of 25 any_email values are allowed.",
            },
        )

    params = ListThreadsParams(
        limit=limit,
        page_token=page_token,
        in_=in_,
        from_=from_,
        to=to,
        cc=cc,
        bcc=bcc,
        any_email=parsed_any_email,
        subject=subject,
        latest_message_after=latest_message_after,
        latest_message_before=latest_message_before,
        unread=unread,
        starred=starred,
        has_attachment=has_attachment,
        search_query_native=search_query_native,
    )

    try:
        result = await registry.get_client(account).list_threads(account, params)
        return ThreadListResponse(request_id=str(uuid.uuid4()), data=result.threads, next_cursor=result.next_cursor)
    except ProviderError as e:
        return provider_error_response(e)
    except Exception:
        logger.exception(f"Failed to list threads for grant {grant_id}")
        return create_error_response(
            error_type="provider_error",
            message="Failed to list threads",
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            provider_error={
                "code": "InternalError",
                "message": "An unexpected error occurred when listing threads",
            },
        )
