import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock
from typing import cast

import pytest

from app.controllers.notifications.incoming_controller import IncomingNotificationController
from app.models.account import AccountProvider, AccountStatus
from app.controllers.providers.exceptions import ProviderError, ProviderRateLimitError
from app.models.job import JobType
from settings import settings


def _controller() -> tuple[IncomingNotificationController, AsyncMock, AsyncMock, AsyncMock, AsyncMock, AsyncMock]:
    account_repo = AsyncMock()
    email_repo = AsyncMock()
    job_repo = AsyncMock()
    gmail = AsyncMock()
    graph = AsyncMock()
    controller = IncomingNotificationController(account_repo, email_repo, job_repo, gmail, graph, AsyncMock())
    return controller, account_repo, email_repo, job_repo, gmail, graph


def test_index_refreshes_deleted_threads_once_when_enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "use_thread_index", True)
    controller, _, _, _, gmail, _ = _controller()
    account = SimpleNamespace(id=1, provider_context={"history_id": "100"})
    gmail.list_history.return_value = {
        "historyId": "102",
        "history": [
            {
                "id": "102",
                "messagesDeleted": [
                    {"message": {"id": "deleted-1", "threadId": "thread"}},
                    {"message": {"id": "deleted-2", "threadId": "thread"}},
                ],
            }
        ],
    }
    asyncio.run(controller.catch_up_google_history(account, "102"))
    index = cast(AsyncMock, controller._mailbox_thread_controller)
    index.refresh_threads.assert_awaited_once_with(account, ["thread"])


def test_google_history_quota_error_reaches_durable_job_retry() -> None:
    controller, account_repo, _, job_repo, gmail, _ = _controller()
    account_repo.get_all_by_email_and_provider.return_value = [
        SimpleNamespace(
            id=1, email="owner@example.com", status=AccountStatus.active, provider_context={"history_id": "100"}
        )
    ]
    gmail.list_history.side_effect = ProviderRateLimitError("Quota exceeded")
    with pytest.raises(ProviderRateLimitError):
        asyncio.run(controller.process_google_notification("owner@example.com", "101"))
    account_repo.update.assert_not_awaited()
    job_repo.enqueue.assert_not_awaited()


@pytest.mark.parametrize("provider", [AccountProvider.google, AccountProvider.microsoft])
def test_message_quota_error_propagates_without_acknowledging_delivery(provider: AccountProvider) -> None:
    controller, _, _, job_repo, gmail, graph = _controller()
    client = gmail if provider == AccountProvider.google else graph
    client.get_message.side_effect = ProviderRateLimitError("Quota exceeded")
    with pytest.raises(ProviderRateLimitError):
        asyncio.run(
            controller._emit_message_event(
                SimpleNamespace(id=1, email="owner@example.com"), "message", provider, "message.created"
            )
        )
    job_repo.enqueue.assert_not_awaited()


@pytest.mark.parametrize("provider", [AccountProvider.google, AccountProvider.microsoft])
@pytest.mark.parametrize(
    "error,delay",
    [
        (ProviderError("Provider unavailable", status_code=503), 5),
        (TimeoutError(), 5),
        (ProviderRateLimitError("Quota exceeded", retry_after_seconds=900), 900),
    ],
)
def test_cache_failure_defers_repair_without_blocking_webhooks(
    provider: AccountProvider, error: Exception, delay: int
) -> None:
    controller, account_repo, _, job_repo, gmail, graph = _controller()
    account = SimpleNamespace(
        id=1,
        email="owner@example.com",
        status=AccountStatus.active,
        provider_context={"history_id": "100", "client_state": "secret"},
    )
    now = datetime(2026, 10, 29, tzinfo=UTC)
    job_repo.db_now.return_value = now
    index = cast(AsyncMock, controller._mailbox_thread_controller)
    index.refresh_threads.side_effect = error
    message = SimpleNamespace(
        id="message", thread_id="thread", folders=["INBOX"], model_dump=lambda **_: {"id": "message"}
    )
    if provider == AccountProvider.google:
        account_repo.get_all_by_email_and_provider.return_value = [account]
        gmail.get_message.return_value = message
        gmail.list_history.return_value = {
            "historyId": "101",
            "history": [{"id": "101", "labelsAdded": [{"message": {"id": "message", "threadId": "thread"}}]}],
        }
        asyncio.run(controller.process_google_notification(account.email, "101"))
        assert account_repo.update.await_args.args[1]["provider_context"]["history_id"] == "101"
    else:
        account_repo.get_by_subscription_id.return_value = account
        graph.get_message.return_value = message
        asyncio.run(
            controller.process_microsoft_notification(
                {
                    "subscriptionId": "subscription",
                    "clientState": "secret",
                    "changeType": "updated",
                    "resourceData": {"id": "message"},
                }
            )
        )
    repair, webhook = job_repo.enqueue.await_args_list
    assert repair.args == (JobType.mailbox_thread_index, {"account_id": 1, "thread_ids": ["thread"]})
    assert repair.kwargs["available_at"] == now + timedelta(seconds=delay)
    assert webhook.args[0] == JobType.webhook_delivery
    assert webhook.args[1]["event_type"] == "message.updated"


def test_google_history_emits_created_and_updated_without_duplicates() -> None:
    controller, account_repo, _, _, gmail, _ = _controller()
    account = SimpleNamespace(
        id=1,
        email="owner@example.com",
        provider_context={"history_id": "100"},
    )
    gmail.list_history.return_value = {
        "historyId": "105",
        "history": [
            {
                "id": "101",
                "messagesAdded": [{"message": {"id": "created-1", "labelIds": ["INBOX", "UNREAD"]}}],
                "labelsAdded": [
                    {"message": {"id": "created-1", "labelIds": ["INBOX", "UNREAD"]}, "labelIds": ["UNREAD"]},
                    {"message": {"id": "updated-1", "labelIds": ["INBOX", "UNREAD"]}, "labelIds": ["UNREAD"]},
                ],
            },
            {
                "id": "105",
                "labelsRemoved": [
                    {"message": {"id": "updated-2", "labelIds": ["INBOX"]}, "labelIds": ["UNREAD"]},
                    {"message": {"id": "draft-1", "labelIds": ["DRAFT"]}, "labelIds": ["UNREAD"]},
                ],
            },
        ],
    }
    controller._emit_message_event = AsyncMock()  # type: ignore[method-assign]

    asyncio.run(controller.catch_up_google_history(account, "105"))

    assert controller._emit_message_event.await_args_list == [
        (
            (account, "created-1"),
            {"provider": AccountProvider.google, "event_type": "message.created"},
        ),
        (
            (account, "updated-1"),
            {"provider": AccountProvider.google, "event_type": "message.updated"},
        ),
        (
            (account, "updated-2"),
            {"provider": AccountProvider.google, "event_type": "message.updated"},
        ),
    ]
    account_repo.update.assert_awaited_once()
    assert account.provider_context["history_id"] == "100"
    assert account_repo.update.await_args.args[1]["provider_context"]["history_id"] == "105"


def test_microsoft_notification_maps_graph_change_type_to_nylas_event() -> None:
    controller, account_repo, _, _, _, _ = _controller()
    account = SimpleNamespace(
        id=1,
        email="owner@example.com",
        status=AccountStatus.active,
        provider_context={"client_state": "secret"},
    )
    account_repo.get_by_subscription_id.return_value = account
    controller._emit_message_event = AsyncMock()  # type: ignore[method-assign]

    asyncio.run(
        controller.process_microsoft_notification(
            {
                "subscriptionId": "subscription-1",
                "clientState": "secret",
                "changeType": "updated",
                "resourceData": {"id": "message-1"},
            }
        )
    )

    controller._emit_message_event.assert_awaited_once_with(
        account,
        "message-1",
        provider=AccountProvider.microsoft,
        event_type="message.updated",
    )


@pytest.mark.parametrize("valid_state", [True, False])
def test_microsoft_deletion_updates_index_after_checking_notification_identity(valid_state: bool) -> None:
    controller, account_repo, _, _, _, _ = _controller()
    account = SimpleNamespace(id=1, status=AccountStatus.active, provider_context={"client_state": "secret"})
    account_repo.get_by_subscription_id.return_value = account
    asyncio.run(
        controller.process_microsoft_notification(
            {
                "subscriptionId": "subscription",
                "clientState": "secret" if valid_state else "wrong",
                "changeType": "deleted",
                "resourceData": {"id": "deleted-message"},
            }
        )
    )
    index = cast(AsyncMock, controller._mailbox_thread_controller)
    if valid_state:
        index.refresh_microsoft_message.assert_awaited_once_with(account, "deleted-message")
    else:
        index.refresh_microsoft_message.assert_not_awaited()


def test_microsoft_update_refreshes_cache_before_emitting_webhook() -> None:
    controller, _, _, job_repo, _, graph = _controller()
    account = SimpleNamespace(id=1, email="owner@example.com")
    message = SimpleNamespace(
        id="message", thread_id="thread", folders=["inbox-id"], model_dump=lambda **_: {"id": "message"}
    )
    graph.get_message.return_value = message
    asyncio.run(controller._emit_message_event(account, "message", AccountProvider.microsoft, "message.updated"))
    index = cast(AsyncMock, controller._mailbox_thread_controller)
    index.refresh_threads.assert_awaited_once_with(account, ["thread"])
    graph.get_message.assert_awaited_once_with(account, "message", include_headers=True)
    job_repo.enqueue.assert_awaited_once()


def test_updated_message_does_not_run_bounce_detection() -> None:
    controller, _, _, job_repo, gmail, _ = _controller()
    account = SimpleNamespace(id=1, email="owner@example.com")
    message = SimpleNamespace(
        id="message-1",
        thread_id="thread-1",
        folders=["INBOX"],
        model_dump=lambda **_: {
            "id": "message-1",
            "thread_id": "thread-1",
            "unread": False,
        },
    )
    gmail.get_message.return_value = message

    asyncio.run(
        controller._emit_message_event(
            account,
            "message-1",
            provider=AccountProvider.google,
            event_type="message.updated",
        )
    )

    job_repo.enqueue.assert_awaited_once()
    assert job_repo.enqueue.await_args.args[1]["event_type"] == "message.updated"
