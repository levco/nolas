"""
Providers router - provider detection for email addresses (Nylas POST /v3/providers/detect).
"""

import logging
import uuid

from fastapi import APIRouter, Depends, Query

from app.api.middlewares.authentication import get_current_app
from app.api.payloads.error import APIError
from app.api.payloads.grants import ProviderDetectData, ProviderDetectResponse
from app.models.app import App

logger = logging.getLogger(__name__)
router = APIRouter()

GOOGLE_MX_MARKERS = ("google.com", "googlemail.com")
MICROSOFT_MX_MARKERS = ("protection.outlook.com", "olc.protection.outlook.com", "outlook.com")

GOOGLE_DOMAINS = {"gmail.com", "googlemail.com"}
MICROSOFT_DOMAINS = {"outlook.com", "hotmail.com", "live.com", "msn.com"}


async def _detect_provider_via_mx(domain: str) -> str | None:
    try:
        import dns.asyncresolver

        resolver = dns.asyncresolver.Resolver()
        resolver.lifetime = 5
        answers = await resolver.resolve(domain, "MX")
        exchanges = [str(record.exchange).rstrip(".").lower() for record in answers]
    except Exception:
        logger.warning(f"MX lookup failed for domain {domain}", exc_info=True)
        return None

    for exchange in exchanges:
        if any(exchange.endswith(marker) for marker in GOOGLE_MX_MARKERS):
            return "google"
    for exchange in exchanges:
        if any(exchange.endswith(marker) for marker in MICROSOFT_MX_MARKERS):
            return "microsoft"
    if exchanges:
        return "imap"
    return None


@router.post(
    "/detect",
    response_model=ProviderDetectResponse,
    responses={400: {"model": APIError, "description": "Invalid email address"}},
    summary="Detect email provider",
    description="Detects the email provider for an address using well-known domains and MX records",
)
async def detect_provider(
    email: str = Query(..., description="Email address to detect the provider for"),
    all_provider_types: bool = Query(False),
    app: App = Depends(get_current_app),
) -> ProviderDetectResponse:
    normalized = email.strip().lower()
    domain = normalized.split("@")[-1] if "@" in normalized else ""

    provider: str | None = None
    if domain in GOOGLE_DOMAINS:
        provider = "google"
    elif domain in MICROSOFT_DOMAINS:
        provider = "microsoft"
    elif domain:
        provider = await _detect_provider_via_mx(domain)

    return ProviderDetectResponse(
        request_id=str(uuid.uuid4()),
        data=ProviderDetectData(email_address=normalized, provider=provider, detected=provider is not None),
    )
