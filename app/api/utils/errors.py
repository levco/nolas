import uuid
from typing import TypedDict

from dependency_injector.wiring import Provide, inject
from fastapi import status
from fastapi.responses import JSONResponse

from app.api.models.error import APIError, ErrorDetail
from app.container import ApplicationContainer
from app.models.account import Account
from app.repos.account import AccountRepo


class _ProviderError(TypedDict):
    code: str
    message: str


def create_error_response(
    error_type: str, message: str, status_code: int, provider_error: _ProviderError | None = None
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
    error_response = APIError(
        request_id=str(uuid.uuid4()), error=ErrorDetail(type=error_type, message=message, provider_error=provider_error)
    )
    return JSONResponse(status_code=status_code, content=error_response.model_dump())


@inject
async def validate_grant_access(
    app_id: int, grant_id: str, account_repo: AccountRepo = Provide[ApplicationContainer.repos.account]
) -> tuple[Account | None, JSONResponse | None]:
    """
    Validate that the grant exists and is accessible for the given app.

    Args:
        app_id: The application ID
        grant_id: The grant ID to validate
        account_repo: The account repository instance (injected)

    Returns:
        Tuple of (account, error_response) where one will be None
    """
    account = await account_repo.get_by_app_and_uuid(app_id, grant_id)
    if account is None:
        error_response = create_error_response(
            error_type="invalid_request_error", message="Invalid grant", status_code=status.HTTP_400_BAD_REQUEST
        )
        return None, error_response
    return account, None
