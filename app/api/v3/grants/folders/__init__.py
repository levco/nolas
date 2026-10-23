import logging
import uuid
from collections.abc import Callable, Coroutine
from email.errors import HeaderParseError
from email.headerregistry import Address
from typing import Annotated, Any

from dependency_injector.wiring import Provide, inject
from fastapi import APIRouter, Depends, Path, Query, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, Response
from fastapi.routing import APIRoute

from app.api.middlewares.authentication import get_current_app
from app.api.payloads.error import APIError
from app.api.payloads.folders import Folder, FolderListResponse, FolderResponse
from app.api.utils.errors import create_error_response, provider_error_response, validate_grant_access
from app.container import ApplicationContainer
from app.controllers.providers.base import ListFoldersParams
from app.controllers.providers.exceptions import ProviderError
from app.controllers.providers.registry import ProviderRegistry
from app.models.account import AccountProvider
from app.models.app import App

logger = logging.getLogger(__name__)


class _FolderRoute(APIRoute):
    def get_route_handler(self) -> Callable[[Request], Coroutine[Any, Any, Response]]:
        handler = super().get_route_handler()

        async def handle(request: Request) -> Response:
            try:
                return await handler(request)
            except RequestValidationError as exc:
                error = exc.errors()[0]
                return create_error_response(
                    "invalid_request_error", f"Invalid {error['loc'][-1]}: {error['msg']}", 400
                )

        return handle


router = APIRouter(route_class=_FolderRoute)


@router.get(
    "",
    response_model=FolderListResponse,
    response_model_exclude_none=True,
    responses={400: {"model": APIError}, 404: {"model": APIError}, 429: {"model": APIError}, 500: {"model": APIError}},
    summary="List all folders",
)
@inject
async def list_folders(
    grant_id: str,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    page_token: str | None = None,
    include_hidden_folders: bool = False,
    parent_id: str | None = None,
    shared_from: str | None = None,
    single_level: bool = False,
    select: str | None = None,
    app: App = Depends(get_current_app),
    registry: ProviderRegistry = Depends(Provide[ApplicationContainer.controllers.provider_registry]),
) -> FolderListResponse | JSONResponse:
    account, error = await validate_grant_access(app.id, grant_id)
    if error:
        return error
    assert account is not None

    if page_token is not None and (len(page_token) > 18 or not page_token.isascii() or not page_token.isdecimal()):
        return create_error_response("invalid_request_error", "Invalid page_token.", 400)
    offset = int(page_token) if page_token is not None else 0
    fields = {name.strip() for name in select.split(",")} if select is not None else None
    if fields is not None and not fields <= Folder.model_fields.keys():
        return create_error_response("invalid_request_error", "Unknown folder field in select.", 400)

    if shared_from is not None and account.provider == AccountProvider.microsoft:
        try:
            if "@" in shared_from:
                address = Address(addr_spec=shared_from)
                if not address.username or not address.domain:
                    raise ValueError("Missing email username or domain")
                shared_from = address.addr_spec
            else:
                uuid.UUID(shared_from)
        except (ValueError, HeaderParseError):
            return create_error_response(
                "invalid_request_error", "shared_from must be an email address or grant ID.", 400
            )
        if "@" not in shared_from:
            shared_account, error = await validate_grant_access(app.id, shared_from)
            if error:
                return error
            assert shared_account is not None
            shared_from = shared_account.email

    try:
        # shortcut: reload folders for each page; use provider cursors if large mailboxes make this costly.
        folders = await registry.get_client(account).list_folders(
            account,
            ListFoldersParams(
                include_hidden_folders=include_hidden_folders,
                parent_id=parent_id,
                shared_from=shared_from,
                single_level=single_level,
            ),
        )
        response = FolderListResponse(
            data=folders[offset : offset + limit],
            next_cursor=str(offset + limit) if offset + limit < len(folders) else None,
        )
        if fields is not None:
            return JSONResponse(
                content=response.model_dump(
                    include={"request_id": True, "data": {"__all__": fields}, "next_cursor": True}, exclude_none=True
                )
            )
        return response
    except ProviderError as exc:
        return provider_error_response(exc)
    except Exception:
        logger.exception("Failed to list folders")
        return create_error_response("provider_error", "Failed to list folders", 500)


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
