from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import cast
from unittest.mock import AsyncMock

import pytest

from app.controllers.jobs.processor import JobProcessorController
from app.models.account import AccountProvider, AccountStatus
from app.models.job import Job, JobType
from settings import settings


def _make_controller() -> tuple[JobProcessorController, AsyncMock, AsyncMock, AsyncMock, AsyncMock, AsyncMock]:
    job_repo = AsyncMock()
    incoming_notification_controller = AsyncMock()
    subscription_manager = AsyncMock()
    account_repo = AsyncMock()
    webhook_sender = AsyncMock()
    controller = JobProcessorController(
        job_repo=job_repo,
        incoming_notification_controller=incoming_notification_controller,
        subscription_manager=subscription_manager,
        account_repo=account_repo,
        webhook_sender=webhook_sender,
    )
    return controller, job_repo, incoming_notification_controller, subscription_manager, account_repo, webhook_sender


class TestJobProcessorController:
    @pytest.mark.asyncio
    async def test_enqueues_google_notification_lowercasing_email(self) -> None:
        controller, job_repo, _, _, account_repo, _ = _make_controller()
        account_repo.get_all_by_email_and_provider.return_value = [SimpleNamespace(status=AccountStatus.active)]
        await controller.enqueue_google_notification("User@Example.COM", "123")
        account_repo.get_all_by_email_and_provider.assert_awaited_once_with("user@example.com", AccountProvider.google)
        job_repo.enqueue.assert_awaited_once_with(
            JobType.google_notification, {"email_address": "user@example.com", "history_id": "123"}
        )

    @pytest.mark.asyncio
    async def test_skips_google_enqueue_without_active_account(self) -> None:
        controller, job_repo, _, _, account_repo, _ = _make_controller()
        account_repo.get_all_by_email_and_provider.return_value = [SimpleNamespace(status=AccountStatus.expired)]

        job = await controller.enqueue_google_notification("user@example.com", "123")

        assert job is None
        job_repo.enqueue.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_enqueues_microsoft_notification_when_active_account_exists(self) -> None:
        controller, job_repo, _, _, account_repo, _ = _make_controller()
        notification = {"subscriptionId": "sub-1", "resourceData": {"id": "msg-1"}}
        account_repo.get_by_subscription_id.return_value = SimpleNamespace(status=AccountStatus.active)

        await controller.enqueue_microsoft_notification(notification)

        account_repo.get_by_subscription_id.assert_awaited_once_with("sub-1")
        job_repo.enqueue.assert_awaited_once_with(JobType.microsoft_notification, {"notification": notification})

    @pytest.mark.asyncio
    async def test_skips_microsoft_enqueue_without_active_account(self) -> None:
        controller, job_repo, _, _, account_repo, _ = _make_controller()
        notification = {"subscriptionId": "sub-1", "resourceData": {"id": "msg-1"}}
        account_repo.get_by_subscription_id.return_value = SimpleNamespace(status=AccountStatus.expired)

        job = await controller.enqueue_microsoft_notification(notification)

        assert job is None
        job_repo.enqueue.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_enqueues_due_subscription_renewals_from_sql_query(self) -> None:
        controller, job_repo, _, _, account_repo, _ = _make_controller()
        account_repo.get_ids_needing_subscription_renewal.return_value = [101, 202]

        enqueued = await controller.enqueue_due_subscription_renewals(renew_within_seconds=3600)

        assert enqueued == 2
        account_repo.get_ids_needing_subscription_renewal.assert_awaited_once_with(3600)
        assert job_repo.enqueue.await_count == 2
        job_repo.enqueue.assert_any_await(JobType.subscription_renewal, {"account_id": 101})
        job_repo.enqueue.assert_any_await(JobType.subscription_renewal, {"account_id": 202})

    @pytest.mark.asyncio
    async def test_processes_subscription_renewal_check_job(self) -> None:
        controller, job_repo, _, _, account_repo, _ = _make_controller()
        now = datetime(2026, 1, 1, tzinfo=timezone.utc)
        job = cast(
            Job,
            SimpleNamespace(
                id=13,
                type=JobType.subscription_renewal_check,
                payload={},
                attempts=0,
                max_attempts=1000,
            ),
        )
        account_repo.get_ids_needing_subscription_renewal.return_value = [101, 202]
        job_repo.db_now.return_value = now
        job_repo.requeue_stale_processing.return_value = 0
        job_repo.claim_batch.return_value = [job]

        processed = await controller.process_available_jobs(worker_id="w-1", batch_size=10, lock_timeout_seconds=300)

        assert processed == 1
        account_repo.get_ids_needing_subscription_renewal.assert_awaited_once_with(
            settings.subscription_renewal.renew_within_hours * 3600
        )
        job_repo.enqueue.assert_any_await(JobType.subscription_renewal, {"account_id": 101})
        job_repo.enqueue.assert_any_await(JobType.subscription_renewal, {"account_id": 202})
        job_repo.enqueue.assert_any_await(
            JobType.subscription_renewal_check,
            {},
            max_attempts=5,
            available_at=now + timedelta(hours=1),
        )
        job_repo.mark_completed.assert_awaited_once_with(job)

    @pytest.mark.asyncio
    async def test_processes_google_job(self) -> None:
        controller, job_repo, incoming_notification_controller, _, _, _ = _make_controller()
        job = cast(
            Job,
            SimpleNamespace(
                id=10,
                type=JobType.google_notification,
                payload={"email_address": "a@b.co", "history_id": "42"},
                attempts=0,
                max_attempts=5,
            ),
        )
        job_repo.requeue_stale_processing.return_value = 0
        job_repo.claim_batch.return_value = [job]

        processed = await controller.process_available_jobs(worker_id="w-1", batch_size=10, lock_timeout_seconds=300)

        assert processed == 1
        incoming_notification_controller.process_google_notification.assert_awaited_once_with("a@b.co", "42")
        job_repo.mark_completed.assert_awaited_once_with(job)
        job_repo.mark_retry_or_failed.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_processes_microsoft_job(self) -> None:
        controller, job_repo, incoming_notification_controller, _, _, _ = _make_controller()
        notification = {"subscriptionId": "sub-1", "resourceData": {"id": "msg-1"}}
        job = cast(
            Job,
            SimpleNamespace(
                id=11,
                type=JobType.microsoft_notification,
                payload={"notification": notification},
                attempts=1,
                max_attempts=5,
            ),
        )
        job_repo.requeue_stale_processing.return_value = 0
        job_repo.claim_batch.return_value = [job]

        processed = await controller.process_available_jobs(worker_id="w-1", batch_size=10, lock_timeout_seconds=300)

        assert processed == 1
        incoming_notification_controller.process_microsoft_notification.assert_awaited_once_with(notification)
        job_repo.mark_completed.assert_awaited_once_with(job)

    @pytest.mark.asyncio
    async def test_marks_retry_when_dispatch_fails(self) -> None:
        controller, job_repo, incoming_notification_controller, _, _, _ = _make_controller()
        job = cast(
            Job,
            SimpleNamespace(
                id=12,
                type=JobType.google_notification,
                payload={"email_address": "a@b.co", "history_id": "42"},
                attempts=0,
                max_attempts=5,
            ),
        )
        job_repo.requeue_stale_processing.return_value = 0
        job_repo.claim_batch.return_value = [job]
        incoming_notification_controller.process_google_notification.side_effect = RuntimeError("boom")

        processed = await controller.process_available_jobs(worker_id="w-1", batch_size=10, lock_timeout_seconds=300)

        assert processed == 1
        job_repo.mark_completed.assert_not_awaited()
        job_repo.mark_retry_or_failed.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_processes_webhook_delivery_job(self) -> None:
        controller, job_repo, _, _, account_repo, webhook_sender = _make_controller()
        account_repo.get_by_id_with_app.return_value = SimpleNamespace(email="user@example.com")
        webhook_sender.send_event.return_value = True

        payload = {
            "account_id": 123,
            "event_type": "message.created",
            "source": "nolas",
            "object_data": {"id": "msg-1"},
            "email_id": "msg-1",
            "thread_id": "thread-1",
        }
        job = cast(
            Job,
            SimpleNamespace(
                id=20,
                type=JobType.webhook_delivery,
                payload=payload,
                attempts=0,
                max_attempts=10,
            ),
        )
        job_repo.requeue_stale_processing.return_value = 0
        job_repo.claim_batch.return_value = [job]

        processed = await controller.process_available_jobs(worker_id="w-1", batch_size=10, lock_timeout_seconds=300)

        assert processed == 1
        account_repo.get_by_id_with_app.assert_awaited_once_with(123)
        webhook_sender.send_event.assert_awaited_once_with(
            account=account_repo.get_by_id_with_app.return_value,
            event_type="message.created",
            object_data={"id": "msg-1"},
            source="nolas",
            email_id="msg-1",
            thread_id="thread-1",
            max_retries=1,
        )
        job_repo.mark_completed.assert_awaited_once_with(job)
