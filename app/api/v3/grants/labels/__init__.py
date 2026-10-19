from dependency_injector.wiring import Provide, inject
from fastapi import APIRouter, Depends, Response
from fastapi.responses import JSONResponse

from app.api.middlewares.authentication import get_current_app
from app.api.payloads.labels import CreateLabelRequest, LabelListResponse, LabelResponse, UpdateLabelRequest
from app.api.utils.errors import provider_error_response, validate_grant_access
from app.container import ApplicationContainer
from app.controllers.providers.exceptions import ProviderError
from app.controllers.providers.registry import ProviderRegistry
from app.models.app import App

router = APIRouter()


@router.get("", response_model=LabelListResponse)
@inject
async def list_labels(
    grant_id: str,
    app: App = Depends(get_current_app),
    registry: ProviderRegistry = Depends(Provide[ApplicationContainer.controllers.provider_registry]),
) -> LabelListResponse | JSONResponse:
    account, error = await validate_grant_access(app.id, grant_id)
    if error:
        return error
    assert account is not None
    try:
        return LabelListResponse(data=await registry.get_client(account).list_labels(account))
    except ProviderError as exc:
        return provider_error_response(exc)


@router.post("", response_model=LabelResponse, status_code=201)
@inject
async def create_label(
    grant_id: str,
    request: CreateLabelRequest,
    app: App = Depends(get_current_app),
    registry: ProviderRegistry = Depends(Provide[ApplicationContainer.controllers.provider_registry]),
) -> LabelResponse | JSONResponse:
    account, error = await validate_grant_access(app.id, grant_id)
    if error:
        return error
    assert account is not None
    try:
        return LabelResponse(data=await registry.get_client(account).create_label(account, request))
    except ProviderError as exc:
        return provider_error_response(exc)


@router.get("/{label_id:path}", response_model=LabelResponse)
@inject
async def get_label(
    grant_id: str,
    label_id: str,
    app: App = Depends(get_current_app),
    registry: ProviderRegistry = Depends(Provide[ApplicationContainer.controllers.provider_registry]),
) -> LabelResponse | JSONResponse:
    account, error = await validate_grant_access(app.id, grant_id)
    if error:
        return error
    assert account is not None
    try:
        return LabelResponse(data=await registry.get_client(account).get_label(account, label_id))
    except ProviderError as exc:
        return provider_error_response(exc)


@router.put("/{label_id:path}", response_model=LabelResponse)
@inject
async def update_label(
    grant_id: str,
    label_id: str,
    request: UpdateLabelRequest,
    app: App = Depends(get_current_app),
    registry: ProviderRegistry = Depends(Provide[ApplicationContainer.controllers.provider_registry]),
) -> LabelResponse | JSONResponse:
    account, error = await validate_grant_access(app.id, grant_id)
    if error:
        return error
    assert account is not None
    try:
        return LabelResponse(data=await registry.get_client(account).update_label(account, label_id, request))
    except ProviderError as exc:
        return provider_error_response(exc)


@router.delete("/{label_id:path}", status_code=204)
@inject
async def delete_label(
    grant_id: str,
    label_id: str,
    app: App = Depends(get_current_app),
    registry: ProviderRegistry = Depends(Provide[ApplicationContainer.controllers.provider_registry]),
) -> Response:
    account, error = await validate_grant_access(app.id, grant_id)
    if error:
        return error
    assert account is not None
    try:
        await registry.get_client(account).delete_label(account, label_id)
        return Response(status_code=204)
    except ProviderError as exc:
        return provider_error_response(exc)
