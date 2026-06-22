"""
Notifications router - inbound push notifications from Google Pub/Sub and Microsoft Graph.

These endpoints are called by providers (and an internal scheduler endpoint), not by
API clients, so they do not use bearer-token app authentication. Google pushes are
verified with Pub/Sub OIDC JWTs; Microsoft notifications are verified via the
per-subscription clientState.
"""

import base64
import json
import logging

from dependency_injector.wiring import Provide, inject
from fastapi import APIRouter, Depends, Header, Query, Request, Response, status
from fastapi.responses import JSONResponse, PlainTextResponse

from app.container import ApplicationContainer
from app.controllers.jobs.processor import JobProcessorController
from app.services.google_oidc import GooglePubSubOidcVerifier
from settings import settings

logger = logging.getLogger(__name__)
router = APIRouter()


@router.post(
    "/google",
    summary="Google Pub/Sub push endpoint",
    description="Receives Gmail watch notifications pushed by Google Pub/Sub",
)
@inject
async def google_notification(
    request: Request,
    authorization: str | None = Header(default=None, alias="Authorization"),
    job_processor: JobProcessorController = Depends(Provide[ApplicationContainer.controllers.job_processor]),
    google_oidc_verifier: GooglePubSubOidcVerifier = Depends(
        Provide[ApplicationContainer.services.google_oidc_verifier]
    ),
) -> Response:
    auth_result = google_oidc_verifier.verify_authorization_header(authorization)
    if not auth_result.is_valid and auth_result.error == GooglePubSubOidcVerifier.CONFIGURATION_ERROR:
        logger.error("Google Pub/Sub OIDC auth is not configured; rejecting push")
        return JSONResponse(status_code=status.HTTP_403_FORBIDDEN, content={"error": "endpoint not configured"})
    if not auth_result.is_valid:
        logger.warning(f"Rejected Google Pub/Sub push: {auth_result.error}")
        return JSONResponse(status_code=status.HTTP_403_FORBIDDEN, content={"error": "invalid token"})

    try:
        envelope = await request.json()
        message = envelope.get("message", {})
        data = json.loads(base64.b64decode(message.get("data", "")).decode("utf-8"))
        email_address = data.get("emailAddress")
        history_id = data.get("historyId")
    except Exception:
        logger.warning("Malformed Pub/Sub push payload", exc_info=True)
        # Acknowledge malformed messages so Pub/Sub does not retry them forever.
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    if email_address and history_id is not None:
        try:
            await job_processor.enqueue_google_notification(str(email_address), str(history_id))
        except Exception:
            logger.exception("Failed to enqueue Google notification job")
            return Response(status_code=status.HTTP_503_SERVICE_UNAVAILABLE)

    return Response(status_code=status.HTTP_200_OK)


@router.post(
    "/microsoft",
    summary="Microsoft Graph change-notification endpoint",
    description="Receives Microsoft Graph change notifications (and validation handshakes)",
)
@inject
async def microsoft_notification(
    request: Request,
    validation_token: str | None = Query(default=None, alias="validationToken"),
    job_processor: JobProcessorController = Depends(Provide[ApplicationContainer.controllers.job_processor]),
) -> Response:
    # Subscription validation handshake: echo the token as text/plain within 10 seconds.
    if validation_token is not None:
        return PlainTextResponse(content=validation_token, status_code=status.HTTP_200_OK)

    try:
        payload = await request.json()
        notifications = payload.get("value", [])
    except Exception:
        logger.warning("Malformed Graph notification payload", exc_info=True)
        return Response(status_code=status.HTTP_202_ACCEPTED)

    for notification in notifications:
        try:
            await job_processor.enqueue_microsoft_notification(notification)
        except Exception:
            logger.exception("Failed to enqueue Microsoft notification job")
            return Response(status_code=status.HTTP_503_SERVICE_UNAVAILABLE)

    return Response(status_code=status.HTTP_202_ACCEPTED)


@router.post(
    "/subscriptions/renew",
    summary="Enqueue subscription renewals",
    description="Internal endpoint to enqueue renewal jobs for due Google/Microsoft accounts.",
)
@inject
async def enqueue_subscription_renewals(
    api_key: str | None = Header(default=None, alias="X-API-Key"),
    job_processor: JobProcessorController = Depends(Provide[ApplicationContainer.controllers.job_processor]),
) -> Response:
    expected_api_key = settings.subscription_renewal.enqueue_api_key
    if not expected_api_key:
        logger.error("SUBSCRIPTION_RENEWAL_ENQUEUE_API_KEY is not configured; rejecting enqueue endpoint")
        return JSONResponse(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, content={"error": "endpoint not configured"})
    if api_key is None or api_key != expected_api_key:
        return JSONResponse(status_code=status.HTTP_403_FORBIDDEN, content={"error": "invalid api key"})

    try:
        enqueued = await job_processor.enqueue_due_subscription_renewals(
            settings.subscription_renewal.renew_within_hours * 3600
        )
    except Exception:
        logger.exception("Failed to enqueue subscription renewal jobs")
        return JSONResponse(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, content={"error": "enqueue failed"})

    return JSONResponse(status_code=status.HTTP_202_ACCEPTED, content={"enqueued": enqueued})
