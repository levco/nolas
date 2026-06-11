import logging
import uuid

from dependency_injector.wiring import Provide, inject
from fastapi import APIRouter, Depends, Path, status
from fastapi.responses import JSONResponse

from app.api.middlewares.authentication import get_current_app
from app.api.payloads.error import APIError
from app.api.payloads.folders import Folder, FolderResponse
from app.api.utils.errors import create_error_response, provider_error_response, validate_grant_access
from app.container import ApplicationContainer
from app.controllers.providers.exceptions import ProviderError
from app.controllers.providers.registry import ProviderRegistry
from app.models.app import App

logger = logging.getLogger(__name__)
router = APIRouter()


@router.get(
    "/{folder_id}",
    response_model=FolderResponse,
    responses={
        400: {"model": APIError, "description": "Invalid grant"},
        404: {"model": APIError, "description": "Folder not found"},
        500: {"model": APIError, "description": "Internal server error"},
    },
    summary="Get a specific folder",
    description="Gets a specific folder by ID for the specified grant",
)
@inject
async def get_folder(
    grant_id: str = Path(..., example="a3ec500d-126b-4532-a632-7808721b3732"),
    folder_id: str = Path(..., example="Sent"),
    app: App = Depends(get_current_app),
    registry: ProviderRegistry = Depends(Provide[ApplicationContainer.controllers.provider_registry]),
) -> FolderResponse | JSONResponse:
    account, error_response = await validate_grant_access(app.id, grant_id)
    if error_response:
        return error_response
    assert account is not None  # account is guaranteed to be not None when error_response is None

    try:
        folder = await registry.get_client(account).get_folder(account, folder_id)
        if folder is None:
            return create_error_response(
                error_type="not_found_error",
                message="Folder not found",
                status_code=status.HTTP_404_NOT_FOUND,
                provider_error={"code": "NotFoundError", "message": "Folder not found"},
            )
        return FolderResponse(
            request_id=str(uuid.uuid4()),
            data=Folder(
                id=folder.id,
                name=folder.name,
                grant_id=grant_id,
                attributes=folder.attributes,
                # Gmail labels carry a type attribute; user labels are not system folders.
                system_folder="user" not in folder.attributes,
                total_count=folder.total_count,
                unread_count=folder.unread_count,
            ),
        )
    except ProviderError as e:
        return provider_error_response(e)
    except Exception:
        logger.exception(f"Failed to fetch folder {folder_id}")
        return create_error_response(
            error_type="provider_error",
            message="Failed to fetch folder",
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        )
