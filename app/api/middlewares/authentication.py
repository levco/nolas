from typing import Annotated

from dependency_injector.wiring import Provide, inject
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.container import ApplicationContainer
from app.models.app import App
from app.repos.app import AppRepo

# Create security scheme instance
security = HTTPBearer()


@inject
async def get_current_app(
    credentials: Annotated[HTTPAuthorizationCredentials, Depends(security)],
    app_repo: AppRepo = Depends(Provide[ApplicationContainer.repos.app]),
) -> App:
    """
    FastAPI dependency to get the current authenticated app from the Authorization header.

    Args:
        credentials: The HTTP Bearer credentials from the Authorization header
        app_repo: The app repository for database operations

    Returns:
        The authenticated App object

    Raises:
        HTTPException: If authentication fails
    """
    api_key = credentials.credentials

    app = await app_repo.get_by_api_key(api_key)
    if app is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid API key.")

    return app
