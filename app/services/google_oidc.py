import logging
from dataclasses import dataclass
from typing import Any, Callable, Mapping

import requests
from cachecontrol import CacheControl
from google.auth.exceptions import GoogleAuthError
from google.auth.transport.requests import Request as GoogleRequest
from google.oauth2 import id_token

logger = logging.getLogger(__name__)


TokenVerifier = Callable[[str, GoogleRequest, str], Mapping[str, Any]]


@dataclass(frozen=True)
class OidcVerificationResult:
    is_valid: bool
    error: str | None = None


class GooglePubSubOidcVerifier:
    GOOGLE_OIDC_ISSUERS = {"accounts.google.com", "https://accounts.google.com"}
    CONFIGURATION_ERROR = "OIDC verifier not configured"

    def __init__(
        self,
        audience: str,
        service_account_email: str,
        token_verifier: TokenVerifier = id_token.verify_oauth2_token,
        request_adapter: GoogleRequest | None = None,
    ) -> None:
        self._audience = audience.strip()
        self._service_account_email = service_account_email.strip().lower()
        self._token_verifier = token_verifier
        self._request_adapter = request_adapter or self._build_cached_request_adapter()

    def verify_authorization_header(self, authorization: str | None) -> OidcVerificationResult:
        if not self._audience or not self._service_account_email:
            return OidcVerificationResult(is_valid=False, error=self.CONFIGURATION_ERROR)

        if not authorization:
            return OidcVerificationResult(is_valid=False, error="Authorization header missing")

        scheme, _, token = authorization.partition(" ")
        if scheme.lower() != "bearer" or not token:
            return OidcVerificationResult(is_valid=False, error="Authorization header must be Bearer token")

        try:
            claims = self._token_verifier(token, self._request_adapter, self._audience)
        except (ValueError, GoogleAuthError):
            logger.warning("Google Pub/Sub OIDC token verification failed", exc_info=True)
            return OidcVerificationResult(is_valid=False, error="Invalid OIDC token")

        issuer = str(claims.get("iss", ""))
        if issuer not in self.GOOGLE_OIDC_ISSUERS:
            return OidcVerificationResult(is_valid=False, error="Unexpected token issuer")

        token_email = str(claims.get("email", "")).lower()
        if token_email != self._service_account_email:
            return OidcVerificationResult(is_valid=False, error="Unexpected service account identity")

        if claims.get("email_verified") is False:
            return OidcVerificationResult(is_valid=False, error="Service account email is not verified")

        return OidcVerificationResult(is_valid=True)

    @staticmethod
    def _build_cached_request_adapter() -> GoogleRequest:
        """Cache cert fetches used by google-auth token verification."""
        cached_session = CacheControl(requests.Session())
        return GoogleRequest(session=cached_session)
