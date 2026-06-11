"""
Notifications router - inbound push notifications from Google Pub/Sub and Microsoft Graph.

These endpoints are called by the providers, not by API clients, so they do not use
bearer-token app authentication. Google pushes are verified via a shared token query
parameter; Microsoft notifications are verified via the per-subscription clientState.
"""

import base64
import json
import logging

from dependency_injector.wiring import Provide, inject
from fastapi import APIRouter, Depends, Query, Request, Response, status
from fastapi.responses import JSONResponse, PlainTextResponse

from app.container import ApplicationContainer
from app.environment import EnvironmentName
from settings import settings
from app.controllers.notifications.incoming_controller import IncomingNotificationController
from app.controllers.notifications.queue import GOOGLE, MICROSOFT, NotificationJob, NotificationQueue

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
    token: str = Query(default=""),
    controller: IncomingNotificationController = Depends(
        Provide[ApplicationContainer.controllers.incoming_notification_controller]
    ),
    queue: NotificationQueue = Depends(Provide[ApplicationContainer.controllers.notification_queue]),
) -> Response:
    expected_token = controller.google_verification_token
    if not expected_token and settings.environment == EnvironmentName.PRODUCTION:
        logger.error("GOOGLE_PUBSUB_VERIFICATION_TOKEN not configured in production; rejecting push")
        return JSONResponse(status_code=status.HTTP_403_FORBIDDEN, content={"error": "endpoint not configured"})
    if expected_token and token != expected_token:
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
        # Ack immediately and process async. Safe: a lost job is recovered by the
        # per-account history cursor on the next notification.
        job = NotificationJob(
            kind=GOOGLE,
            payload={"email_address": str(email_address).lower(), "history_id": str(history_id)},
        )
        if not queue.try_enqueue(job):
            # Backpressure: Pub/Sub retries on non-2xx.
            return Response(status_code=status.HTTP_503_SERVICE_UNAVAILABLE)

    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post(
    "/microsoft",
    summary="Microsoft Graph change-notification endpoint",
    description="Receives Microsoft Graph change notifications (and validation handshakes)",
)
@inject
async def microsoft_notification(
    request: Request,
    validation_token: str | None = Query(default=None, alias="validationToken"),
    queue: NotificationQueue = Depends(Provide[ApplicationContainer.controllers.notification_queue]),
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
        if not queue.try_enqueue(NotificationJob(kind=MICROSOFT, payload={"notification": notification})):
            # Backpressure: Graph retries on non-2xx.
            return Response(status_code=status.HTTP_503_SERVICE_UNAVAILABLE)

    return Response(status_code=status.HTTP_202_ACCEPTED)
