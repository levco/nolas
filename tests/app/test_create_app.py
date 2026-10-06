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


@pytest.mark.asyncio
@pytest.mark.parametrize("failure_phase", ["provider", "close"])
@pytest.mark.parametrize(
    "failed_resource",
    ["google_http_client", "microsoft_http_client", "token_service", "webhook_sender", "redis_client"],
)
async def test_lifespan_continues_cleanup_after_failure(
    failed_resource: str,
    failure_phase: str,
    caplog: pytest.LogCaptureFixture,
) -> None:
    container = MagicMock()
    resources = {
        "google_http_client": "close",
        "microsoft_http_client": "close",
        "token_service": "close",
        "webhook_sender": "close_session",
        "redis_client": "aclose",
    }
    for name in resources:
        getattr(container.controllers, name).return_value = AsyncMock()
    provider = getattr(container.controllers, failed_resource)
    if failure_phase == "provider":
        provider.side_effect = RuntimeError("initialization failed")
    else:
        getattr(provider.return_value, resources[failed_resource]).side_effect = RuntimeError("close failed")
    app = create_app(container=container)

    async with app.router.lifespan_context(app):
        pass

    for name, close_method in resources.items():
        resource_provider = getattr(container.controllers, name)
        resource_provider.assert_called_once_with()
        if name != failed_resource or failure_phase == "close":
            getattr(resource_provider.return_value, close_method).assert_awaited_once_with()
    assert f"Failed to close {failed_resource} during shutdown" in caplog.text
