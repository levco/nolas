from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.controllers.webhooks.sender import WebhookSender
from app.create_app import create_app


@pytest.mark.asyncio
async def test_lifespan_closes_webhook_session_created_by_grant_event() -> None:
    sender = WebhookSender(AsyncMock())
    container = MagicMock()
    for name in ("google_http_client", "microsoft_http_client", "token_service", "redis_client"):
        getattr(container.controllers, name).return_value = AsyncMock()
    container.controllers.webhook_sender.return_value = sender
    app = create_app(container=container)

    async with app.router.lifespan_context(app):
        # A missing URL still creates the session but avoids sending a network request.
        account = SimpleNamespace(app=SimpleNamespace(id=1, webhook_url=None, grant_webhook_url=None))
        assert not await sender.send_event(account, "grant.expired", {})
        session = sender._http_session
        assert session is not None and not session.closed

    assert session.closed
    assert sender._http_session is None
