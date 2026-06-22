from typing import Any, Mapping

from app.controllers.notifications.google_oidc import CONFIGURATION_ERROR, GooglePubSubOidcVerifier


def _verifier_with_claims(claims: Mapping[str, Any]) -> GooglePubSubOidcVerifier:
    def fake_verifier(token: str, request: object, audience: str) -> Mapping[str, Any]:
        assert token == "token-123"
        assert audience == "nolas-google-pubsub"
        return claims

    return GooglePubSubOidcVerifier(
        audience="nolas-google-pubsub",
        service_account_email="pubsub-sa@lev-nylas-dev.iam.gserviceaccount.com",
        token_verifier=fake_verifier,
    )


class TestGooglePubSubOidcVerifier:
    def test_rejects_when_not_configured(self) -> None:
        verifier = GooglePubSubOidcVerifier(audience="", service_account_email="")
        result = verifier.verify_authorization_header("Bearer token-123")
        assert not result.is_valid
        assert result.error == CONFIGURATION_ERROR

    def test_rejects_missing_authorization_header(self) -> None:
        verifier = _verifier_with_claims({})
        result = verifier.verify_authorization_header(None)
        assert not result.is_valid
        assert result.error == "Authorization header missing"

    def test_rejects_non_bearer_header(self) -> None:
        verifier = _verifier_with_claims({})
        result = verifier.verify_authorization_header("Basic token-123")
        assert not result.is_valid
        assert result.error == "Authorization header must be Bearer token"

    def test_accepts_valid_google_oidc_claims(self) -> None:
        verifier = _verifier_with_claims(
            {
                "iss": "https://accounts.google.com",
                "email": "pubsub-sa@lev-nylas-dev.iam.gserviceaccount.com",
                "email_verified": True,
            }
        )
        result = verifier.verify_authorization_header("Bearer token-123")
        assert result.is_valid

    def test_rejects_unexpected_service_account(self) -> None:
        verifier = _verifier_with_claims(
            {
                "iss": "https://accounts.google.com",
                "email": "wrong-sa@lev-nylas-dev.iam.gserviceaccount.com",
                "email_verified": True,
            }
        )
        result = verifier.verify_authorization_header("Bearer token-123")
        assert not result.is_valid
        assert result.error == "Unexpected service account identity"

    def test_rejects_invalid_issuer(self) -> None:
        verifier = _verifier_with_claims(
            {
                "iss": "https://issuer.example.com",
                "email": "pubsub-sa@lev-nylas-dev.iam.gserviceaccount.com",
                "email_verified": True,
            }
        )
        result = verifier.verify_authorization_header("Bearer token-123")
        assert not result.is_valid
        assert result.error == "Unexpected token issuer"
