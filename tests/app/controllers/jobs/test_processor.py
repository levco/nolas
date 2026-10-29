from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import cast
from unittest.mock import AsyncMock, patch

import pytest

from app.controllers.jobs.processor import JobProcessorController
from app.controllers.providers.exceptions import ProviderRateLimitError
from app.models.account import AccountProvider, AccountStatus
from app.models.job import Job, JobStatus, JobType
from app.repos.job import JobRepo
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
        renewal_alerts=AsyncMock(),
        mailbox_thread_controller=AsyncMock(),
    )
    return controller, job_repo, incoming_notification_controller, subscription_manager, account_repo, webhook_sender


class TestJobProcessorController:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("hint,delay", [(None, 300), (120, 300), (900, 900)])
    async def test_quota_errors_schedule_minutes_later_instead_of_completing(
        self, hint: int | None, delay: int
    ) -> None:
        controller, job_repo, _, _, _, _ = _make_controller()
        error = ProviderRateLimitError("Quota exceeded", retry_after_seconds=hint)
        job = Job(
            id=1,
            type=JobType.mailbox_thread_index,
            status=JobStatus.processing,
            payload={"account_id": 7},
            attempts=0,
            max_attempts=5,
        )
        with patch.object(controller, "_dispatch", AsyncMock(side_effect=error)):
            await controller._process_one(job)
        job_repo.mark_retry_or_failed.assert_awaited_once_with(
            job, str(error), retry_delay_seconds=delay, count_attempt=False
        )
        job_repo.mark_completed.assert_not_awaited()

    @pytest.mark.asyncio
    @pytest.mark.parametrize("attempts", [0, 4])
    async def test_repeated_quota_errors_preserve_failure_budget_and_payload(self, attempts: int) -> None:
        repo = JobRepo()
        now = datetime(2026, 10, 29, tzinfo=timezone.utc)
        job = Job(
            id=1,
            type=JobType.mailbox_thread_index,
            status=JobStatus.processing,
            payload={"account_id": 7},
            attempts=attempts,
            max_attempts=5,
            locked_by="worker",
            locked_at=now,
        )
        with patch.object(repo, "db_now", AsyncMock(return_value=now)), patch.object(repo, "flush", AsyncMock()):
            for _ in range(7):
                job.status = JobStatus.processing
                job.locked_by, job.locked_at = "worker", now
                await repo.mark_retry_or_failed(job, "Quota exceeded", retry_delay_seconds=300, count_attempt=False)
                assert job.status == JobStatus.pending
                assert job.available_at == now + timedelta(minutes=5)
                assert job.payload == {"account_id": 7} and job.attempts == attempts
                assert job.locked_by is None and job.locked_at is None
                assert job.completed_at is None and job.last_error == "Quota exceeded"
            await repo.mark_retry_or_failed(job, "Other failure", retry_delay_seconds=5)
        assert job.attempts == attempts + 1
        assert job.status == (JobStatus.failed if attempts == 4 else JobStatus.pending)

    @pytest.mark.asyncio
    async def test_quota_retry_releases_worker_to_process_the_next_job(self) -> None:
        controller, job_repo, _, _, _, _ = _make_controller()
        now = datetime(2026, 10, 29, tzinfo=timezone.utc)
        limited = Job(
            id=1,
            type=JobType.mailbox_thread_index,
            status=JobStatus.processing,
            payload={"account_id": 7},
            attempts=0,
            max_attempts=5,
            locked_by="worker",
            locked_at=now,
        )
        other = Job(id=2, type=JobType.google_notification, status=JobStatus.processing)
        repo = JobRepo()

        async def retry(job: Job, error: str, retry_delay_seconds: int, *, count_attempt: bool) -> None:
            await repo.mark_retry_or_failed(job, error, retry_delay_seconds, count_attempt=count_attempt)

        async def dispatch(job: Job) -> None:
            if job.id == limited.id:
                raise ProviderRateLimitError("Quota exceeded")
            assert limited.status == JobStatus.pending
            assert limited.available_at == now + timedelta(minutes=5)
            assert limited.locked_at is None and limited.locked_by is None
            assert limited.attempts == 0
            job_repo.commit.assert_awaited_once()

        job_repo.requeue_stale_processing.return_value = 0
        job_repo.claim_batch.return_value = [limited, other]
        job_repo.mark_retry_or_failed.side_effect = retry
        with (
            patch.object(repo, "db_now", AsyncMock(return_value=now)),
            patch.object(repo, "flush", AsyncMock()),
            patch.object(controller, "_dispatch", AsyncMock(side_effect=dispatch)),
        ):
            assert await controller.process_available_jobs("worker", 2, 60) == 2
        job_repo.mark_completed.assert_awaited_once_with(other)
        assert job_repo.commit.await_count == 2

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "job_type,attempts,should_alert",
        [
            (JobType.subscription_renewal, 3, False),
            (JobType.subscription_renewal, 4, True),
            (JobType.webhook_delivery, 4, False),
        ],
    )
    async def test_alerts_only_after_exhausted_renewal_is_committed(
        self, job_type: JobType, attempts: int, should_alert: bool
    ) -> None:
        controller, job_repo, _, _, _, _ = _make_controller()
        alerts = AsyncMock()
        controller._renewal_alerts = alerts
        job = Job(
            id=50,
            type=job_type,
            status=JobStatus.processing,
            payload={"account_id": 123},
            attempts=attempts,
            max_attempts=5,
        )
        job_repo.requeue_stale_processing.return_value = 0
        job_repo.claim_batch.return_value = [job]

        async def mark_failed(job: Job, error: str, retry_delay_seconds: int, *, count_attempt: bool) -> None:
            assert count_attempt
            job.attempts += 1
            job.status = JobStatus.failed if job.attempts == job.max_attempts else JobStatus.pending

        async def alert_after_commit(job: Job) -> None:
            job_repo.commit.assert_awaited_once()

        job_repo.mark_retry_or_failed.side_effect = mark_failed
        alerts.record_failure.side_effect = alert_after_commit
        with patch.object(controller, "_dispatch", AsyncMock(side_effect=RuntimeError("provider unavailable"))):
            assert await controller.process_available_jobs("worker", 5, 60) == 1
        if should_alert:
            alerts.record_failure.assert_awaited_once_with(job)
        else:
            alerts.record_failure.assert_not_awaited()

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
            available_at=now + timedelta(hours=settings.subscription_renewal.check_interval_hours),
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
        assert job_repo.mark_retry_or_failed.await_args.kwargs["count_attempt"] is True

    @pytest.mark.asyncio
    async def test_processes_subscription_renewal_with_app_preloaded(self) -> None:
        controller, job_repo, _, subscription_manager, account_repo, _ = _make_controller()
        account = SimpleNamespace(status=AccountStatus.active)
        account_repo.get_by_id_with_app.return_value = account
        job = cast(
            Job,
            SimpleNamespace(
                id=19,
                type=JobType.subscription_renewal,
                status=JobStatus.pending,
                payload={"account_id": 123},
                attempts=0,
                max_attempts=5,
            ),
        )
        job_repo.requeue_stale_processing.return_value = 0
        job_repo.claim_batch.return_value = [job]

        processed = await controller.process_available_jobs(worker_id="w-1", batch_size=10, lock_timeout_seconds=300)

        assert processed == 1
        account_repo.get_by_id_with_app.assert_awaited_once_with(123)
        subscription_manager.ensure_subscription.assert_awaited_once_with(account)
        job_repo.mark_completed.assert_awaited_once_with(job)

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
