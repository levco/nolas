"""
Grants API router - Parent router for grant-related endpoints.
"""

import logging
import uuid

from dependency_injector.wiring import Provide, inject
from fastapi import APIRouter, Depends, Path, status
from fastapi.responses import JSONResponse

from app.api.middlewares.authentication import get_current_app
from app.api.payloads.error import APIError
from app.api.payloads.grants import DeleteGrantResponse, GrantData, GrantResponse, UpdateGrantRequest
from app.api.utils.errors import create_error_response, provider_error_response, validate_grant_access
from app.container import ApplicationContainer
from app.controllers.grant.custom_auth_controller import CustomAuthController
from app.controllers.grant.grant_controller import GrantController
from app.controllers.notifications.subscription_manager import SubscriptionManager
from app.controllers.providers.exceptions import ProviderError
from app.models.account import Account, AccountProvider
from app.models.app import App

from .attachments import router as attachments_router
from .folders import router as folders_router
from .messages import router as messages_router
from .threads import router as threads_router

logger = logging.getLogger(__name__)
router = APIRouter()

# Include sub-routers under grants
router.include_router(attachments_router, prefix="/{grant_id}/attachments", tags=["attachments"])
router.include_router(messages_router, prefix="/{grant_id}/messages", tags=["messages"])
router.include_router(threads_router, prefix="/{grant_id}/threads", tags=["threads"])
router.include_router(folders_router, prefix="/{grant_id}/folders", tags=["folders"])


def _grant_data(account: Account) -> GrantData:
    return GrantData(
        id=str(account.uuid),
        provider=account.provider.value,
        email=account.email,
        grant_status=account.grant_status,
        created_at=int(account.created_at.timestamp()) if account.created_at else None,
        updated_at=int(account.updated_at.timestamp()) if account.updated_at else None,
    )


@router.get(
    "/{grant_id}",
    response_model=GrantResponse,
    responses={
        404: {"model": APIError, "description": "Grant not found"},
    },
    summary="Get a grant",
    description="Gets grant metadata and status by grant ID",
)
async def get_grant(
    grant_id: str = Path(..., example="a3ec500d-126b-4532-a632-7808721b3732"),
    app: App = Depends(get_current_app),
) -> GrantResponse | JSONResponse:
    account, error_response = await validate_grant_access(app.id, grant_id)
    if error_response:
        return error_response
    assert account is not None  # account is guaranteed to be not None when error_response is None

    return GrantResponse(request_id=str(uuid.uuid4()), data=_grant_data(account))


@router.patch(
    "/{grant_id}",
    response_model=GrantResponse,
    responses={
        400: {"model": APIError, "description": "Invalid request"},
        404: {"model": APIError, "description": "Grant not found"},
    },
    summary="Update a grant",
    description="Updates a grant's refresh token (Nylas custom auth re-authentication)",
)
@inject
async def update_grant(
    update_request: UpdateGrantRequest,
    grant_id: str = Path(..., example="a3ec500d-126b-4532-a632-7808721b3732"),
    app: App = Depends(get_current_app),
    custom_auth_controller: CustomAuthController = Depends(
        Provide[ApplicationContainer.controllers.custom_auth_controller]
    ),
) -> GrantResponse | JSONResponse:
    logger.info(f"Received grant update request for grant {grant_id} and app {app.name}")
    account, error_response = await validate_grant_access(app.id, grant_id)
    if error_response:
        return error_response
    assert account is not None  # account is guaranteed to be not None when error_response is None

    if account.provider not in (AccountProvider.google, AccountProvider.microsoft):
        return create_error_response(
            error_type="invalid_request_error",
            message=f"Grant updates are not supported for provider {account.provider.value}.",
            status_code=status.HTTP_400_BAD_REQUEST,
        )
    if not update_request.settings.refresh_token:
        return create_error_response(
            error_type="invalid_request_error",
            message="settings.refresh_token is required.",
            status_code=status.HTTP_400_BAD_REQUEST,
        )

    try:
        account = await custom_auth_controller.update_grant_refresh_token(
            account, update_request.settings.refresh_token
        )
        return GrantResponse(request_id=str(uuid.uuid4()), data=_grant_data(account))
    except ProviderError as e:
        return provider_error_response(e)
    except Exception:
        logger.exception(f"Failed to update grant {grant_id}")
        return create_error_response(
            error_type="internal_error",
            message="An unexpected error occurred when updating the grant",
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        )


@router.delete(
    "/{grant_id}",
    response_model=DeleteGrantResponse,
    responses={
        400: {"model": APIError, "description": "Invalid grant"},
        404: {"model": APIError, "description": "Grant not found"},
        500: {"model": APIError, "description": "Internal server error"},
    },
    summary="Delete a grant",
    description="Deletes a grant by setting the account status to inactive and removing all associated uid_tracking records",
)
@inject
async def delete_grant(
    grant_id: str = Path(..., example="a3ec500d-126b-4532-a632-7808721b3732"),
    app: App = Depends(get_current_app),
    grant_controller: GrantController = Depends(Provide[ApplicationContainer.controllers.grant_controller]),
    subscription_manager: SubscriptionManager = Depends(Provide[ApplicationContainer.controllers.subscription_manager]),
) -> DeleteGrantResponse | JSONResponse:
    """
    Delete a grant by setting the account status to inactive and removing uid_tracking records.

    Args:
        grant_id: The grant ID to delete
        app: The current application (injected)
        grant_controller: The grant controller instance (injected)

    Returns:
        DeleteGrantResponse or error response
    """
    # Validate that the grant exists and is accessible for the given app
    account, error_response = await validate_grant_access(app.id, grant_id)
    if error_response:
        return error_response
    assert account is not None  # account is guaranteed to be not None when error_response is None

    try:
        if account.provider in (AccountProvider.google, AccountProvider.microsoft):
            await subscription_manager.teardown(account)
        await grant_controller.delete_grant(account)
        return DeleteGrantResponse(request_id=str(uuid.uuid4()), success=True)
    except Exception:
        return create_error_response(
            error_type="internal_error",
            message="An unexpected error occurred when deleting the grant",
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        )
