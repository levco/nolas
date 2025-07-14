from typing import Optional

from pydantic import BaseModel, Field


class OAuth2AuthorizeRequest(BaseModel):
    """OAuth2 authorization request model."""

    client_id: str = Field(..., description="The client ID of the requesting application")
    redirect_uri: str = Field(..., description="The URI to redirect to after authorization")
    state: str = Field(..., description="A random string to prevent CSRF attacks")
    scope: Optional[str] = Field(None, description="The requested scope")
    response_type: str = Field(default="code", description="The response type (must be 'code')")


class OAuth2AuthorizeResponse(BaseModel):
    """OAuth2 authorization response model."""

    request_id: str = Field(..., description="Unique request identifier")
    authorization_url: str = Field(..., description="URL to redirect user for authorization")


class OAuth2CredentialsRequest(BaseModel):
    """OAuth2 credentials request for IMAP account setup."""

    email: str = Field(..., description="Email address of the IMAP account")
    password: str = Field(..., description="Password for the IMAP account")
    imap_host: str = Field(..., description="IMAP server host")
    imap_port: int = Field(default=993, description="IMAP server port")
    use_ssl: bool = Field(default=True, description="Whether to use SSL/TLS")


class OAuth2TokenRequest(BaseModel):
    """OAuth2 token exchange request model."""

    grant_type: str = Field(..., description="The grant type (must be 'authorization_code')")
    code: str = Field(..., description="The authorization code")
    redirect_uri: str = Field(..., description="The redirect URI used in the authorization request")
    client_id: str = Field(..., description="The client ID")


class OAuth2TokenResponse(BaseModel):
    """OAuth2 token exchange response model."""

    request_id: str = Field(..., description="Unique request identifier")
    grant_id: str = Field(..., description="The grant ID (account UUID)")


class OAuth2AuthorizationPageData(BaseModel):
    """Data for the OAuth2 authorization page."""

    client_id: str
    redirect_uri: str
    state: str
    scope: Optional[str] = None
    app_name: str
    request_uuid: str
