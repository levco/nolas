"""
OAuth2 Connect router - Handles OAuth2 authorization flow for IMAP accounts.
"""

import logging
import secrets
import uuid
from pathlib import Path
from urllib.parse import urlencode, urlparse

from dependency_injector.wiring import Provide, inject
from fastapi import APIRouter, Depends, Form, HTTPException, Query, Request, status
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates

from app.api.middlewares.authentication import get_current_app
from app.api.payloads.error import APIError
from app.api.payloads.oauth2 import OAuth2TokenRequest, OAuth2TokenResponse
from app.container import ApplicationContainer
from app.controllers.imap.connection import ConnectionManager
from app.models.account import Account, AccountProvider, AccountStatus
from app.models.app import App
from app.models.oauth2 import OAuth2AuthorizationRequest, OAuth2RequestStatus
from app.repos.account import AccountRepo
from app.repos.app import AppRepo
from app.repos.oauth2 import OAuth2AuthorizationRequestRepo
from app.utils.password import PasswordUtils

logger = logging.getLogger(__name__)
router = APIRouter()

# Initialize templates
templates = Jinja2Templates(directory=Path(__file__).parent / "templates")


def _generate_authorization_code() -> str:
    """Generate a secure authorization code."""
    return secrets.token_urlsafe(32)


def _validate_redirect_uri(redirect_uri: str) -> bool:
    """Validate redirect URI format."""
    try:
        parsed = urlparse(redirect_uri)
        return bool(parsed.scheme in ["http", "https"] and parsed.netloc)
    except Exception:
        return False


async def _test_imap_connection(email: str, password: str, imap_host: str, imap_port: int) -> bool:
    """Test IMAP connection with provided credentials."""
    try:
        # Create a temporary account for testing
        test_account = Account(
            email=email,
            provider=AccountProvider.imap,
            credentials=PasswordUtils.encrypt_password(password),
            provider_context={"imap_host": imap_host, "imap_port": imap_port},
            status=AccountStatus.active,
            app_id=0,  # Temporary
        )

        # Test connection
        connection_manager = ConnectionManager()
        try:
            connection = await connection_manager.get_connection(test_account)
            if connection is None:
                return False
            await connection_manager.close_connection(connection, test_account)
            return True
        except Exception as e:
            logger.warning(f"IMAP connection test failed for {email}: {e}")
            return False
    except Exception as e:
        logger.error(f"Error testing IMAP connection: {e}")
        return False


@router.get(
    "/auth",
    response_class=HTMLResponse,
    responses={
        400: {"model": APIError, "description": "Invalid request parameters"},
        500: {"model": APIError, "description": "Internal server error"},
    },
    summary="OAuth2 Authorization Form",
    description="Display OAuth2 authorization form for IMAP account access",
)
@inject
async def show_auth_form(
    request: Request,
    client_id: str = Query(..., description="Client ID of the requesting application"),
    redirect_uri: str = Query(..., description="Redirect URI for authorization response"),
    state: str = Query(..., description="State parameter for CSRF protection"),
    scope: str = Query(None, description="Requested scope"),
    response_type: str = Query("code", description="Response type (must be 'code')"),
    login_hint: str = Query(None, description="Hint to display in the email input"),
    app_repo: AppRepo = Depends(Provide[ApplicationContainer.repos.app]),
) -> HTMLResponse:
    """
    Display OAuth2 authorization form.

    This endpoint validates the OAuth2 parameters and displays the authorization form
    where users can enter their IMAP credentials.
    """

    # Validate request
    if response_type != "code":
        return HTMLResponse(
            content="<html><body><h1>Error: Unsupported response_type. Must be 'code'.</h1></body></html>",
            status_code=400,
        )

    if not _validate_redirect_uri(redirect_uri):
        return HTMLResponse(
            content="<html><body><h1>Error: Invalid redirect_uri format.</h1></body></html>", status_code=400
        )

    try:
        app_uuid = uuid.UUID(client_id)
        app = await app_repo.get_by_uuid(app_uuid)
        if app is None:
            return HTMLResponse(content="<html><body><h1>Error: Invalid client_id.</h1></body></html>", status_code=400)
    except Exception:
        return HTMLResponse(content="<html><body><h1>Error: Invalid client_id.</h1></body></html>", status_code=400)

    try:
        # Display the authorization form
        return templates.TemplateResponse(
            "authorize_form.html",
            {
                "request": request,
                "app_name": app.name,
                "client_id": client_id,
                "redirect_uri": redirect_uri,
                "state": state,
                "scope": scope,
                "login_hint": login_hint,
            },
        )

    except Exception:
        logger.exception("Error showing authorization form")
        return HTMLResponse(
            content="<html><body><h1>Error: Failed to load authorization form</h1></body></html>", status_code=500
        )


@router.post(
    "/process",
    response_model=None,
    summary="Process Authorization",
    description="Process user authorization with IMAP credentials",
)
@inject
async def process_authorization(
    client_id: str = Form(...),
    redirect_uri: str = Form(...),
    state: str = Form(...),
    scope: str = Form(None),
    email: str = Form(...),
    password: str = Form(...),
    imap_host: str = Form(...),
    imap_port: int = Form(993),
    smtp_host: str = Form(...),
    smtp_port: int = Form(587),
    app_repo: AppRepo = Depends(Provide[ApplicationContainer.repos.app]),
    auth_request_repo: OAuth2AuthorizationRequestRepo = Depends(
        Provide[ApplicationContainer.repos.oauth2_authorization_request]
    ),
    account_repo: AccountRepo = Depends(Provide[ApplicationContainer.repos.account]),
) -> JSONResponse:
    """
    Process the authorization form submission.

    This endpoint:
    1. Validates the OAuth2 parameters and IMAP credentials
    2. Creates or updates the account
    3. Generates an authorization code
    4. Redirects back to the client application
    """

    try:
        # Validate client_id and get app
        try:
            app_uuid = uuid.UUID(client_id)
            app = await app_repo.get_by_uuid(app_uuid)
            if app is None:
                return JSONResponse(content={"success": False, "error": "Invalid client_id"}, status_code=400)
        except Exception:
            return JSONResponse(content={"success": False, "error": "Invalid client_id"}, status_code=400)

        # Validate redirect URI
        if not _validate_redirect_uri(redirect_uri):
            return JSONResponse(content={"success": False, "error": "Invalid redirect_uri format"}, status_code=400)

        # Test IMAP connection
        if not await _test_imap_connection(email, password, imap_host, imap_port):
            return JSONResponse(
                content={
                    "success": False,
                    "error": "Unable to connect to email server. Please check your credentials and try again.",
                },
                status_code=400,
            )

        # Check if account already exists
        existing_account = await account_repo.get_by_email(email)
        if existing_account:
            # Update existing account
            account = await account_repo.update(
                existing_account,
                {
                    "credentials": PasswordUtils.encrypt_password(password),
                    "provider_context": {
                        "imap_host": imap_host,
                        "imap_port": imap_port,
                        "smtp_host": smtp_host,
                        "smtp_port": smtp_port,
                    },
                },
            )
        else:
            account = Account(
                app_id=app.id,
                email=email,
                provider=AccountProvider.imap,
                credentials=PasswordUtils.encrypt_password(password),
                provider_context={
                    "imap_host": imap_host,
                    "imap_port": imap_port,
                    "smtp_host": smtp_host,
                    "smtp_port": smtp_port,
                },
                status=AccountStatus.pending,
            )
            await account_repo.add(account)

        # Generate authorization code
        auth_code = _generate_authorization_code()

        # Create authorization request
        auth_request = OAuth2AuthorizationRequest(
            app_id=app.id,
            client_id=client_id,
            redirect_uri=redirect_uri,
            state=state,
            scope=scope,
            status=OAuth2RequestStatus.authorized,
            code=auth_code,
            account=account,
        )
        await auth_request_repo.add(auth_request)

        redirect_params = {"code": auth_code, "state": state, "source": "nolas"}
        redirect_url = f"{redirect_uri}?{urlencode(redirect_params)}"

        return JSONResponse(content={"success": True, "redirect_url": redirect_url}, status_code=200)

    except Exception:
        logger.exception("Error processing authorization")
        return JSONResponse(
            content={"success": False, "error": "Internal server error during authorization"}, status_code=500
        )


@router.post(
    "/token",
    response_model=OAuth2TokenResponse,
    responses={
        400: {"model": APIError, "description": "Invalid request or authorization code"},
        401: {"model": APIError, "description": "Invalid client credentials"},
        500: {"model": APIError, "description": "Internal server error"},
    },
    summary="Token Exchange",
    description="Exchange authorization code for access token (grant ID)",
)
@inject
async def token_exchange(
    token_request: OAuth2TokenRequest,
    app: App = Depends(get_current_app),
    auth_code_repo: OAuth2AuthorizationRequestRepo = Depends(
        Provide[ApplicationContainer.repos.oauth2_authorization_request]
    ),
    account_repo: AccountRepo = Depends(Provide[ApplicationContainer.repos.account]),
) -> OAuth2TokenResponse:
    """
    Exchange authorization code for access token.

    This endpoint validates the authorization code and returns the grant ID
    which can be used to access the IMAP account through the grants API.
    """

    if token_request.grant_type != "authorization_code":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="Unsupported grant_type. Must be 'authorization_code'."
        )
    if token_request.client_id != str(app.uuid):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid client_id.")

    try:
        auth_code = await auth_code_repo.get_by_code(token_request.code)
        if not auth_code:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid authorization code.")

        if not auth_code.is_valid():
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST, detail="Authorization code expired or already used."
            )
        if auth_code.redirect_uri != token_request.redirect_uri:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid redirect_uri.")
        if auth_code.app_id != app.id:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED, detail="Authorization code not issued for this application."
            )

        await auth_code_repo.mark_as_used(auth_code)
        await account_repo.mark_as_active(auth_code.account)

        return OAuth2TokenResponse(request_id=str(uuid.uuid4()), grant_id=str(auth_code.account.uuid))

    except HTTPException:
        raise
    except Exception as e:
        logger.exception("Error exchanging authorization code for token")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Failed to exchange authorization code for token"
        ) from e
