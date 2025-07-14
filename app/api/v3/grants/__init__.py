"""
Grants API router - Parent router for grant-related endpoints.
"""

import uuid

from dependency_injector.wiring import Provide, inject
from fastapi import APIRouter, Depends, Path, status
from fastapi.responses import JSONResponse

from app.api.middlewares.authentication import get_current_app
from app.api.payloads.error import APIError
from app.api.payloads.grants import DeleteGrantResponse
from app.api.utils.errors import create_error_response, validate_grant_access
from app.container import ApplicationContainer
from app.controllers.grant.grant_controller import GrantController
from app.models.app import App

from .attachments import router as attachments_router
from .folders import router as folders_router
from .messages import router as messages_router

router = APIRouter()

# Include sub-routers under grants
router.include_router(attachments_router, prefix="/{grant_id}/attachments", tags=["attachments"])
router.include_router(messages_router, prefix="/{grant_id}/messages", tags=["messages"])
router.include_router(folders_router, prefix="/{grant_id}/folders", tags=["folders"])


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
        await grant_controller.delete_grant(account)
        return DeleteGrantResponse(request_id=str(uuid.uuid4()), success=True)
    except Exception:
        return create_error_response(
            error_type="internal_error",
            message="An unexpected error occurred when deleting the grant",
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        )
