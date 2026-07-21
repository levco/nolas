import logging
from datetime import datetime, timedelta
from typing import Any

from app.controllers.jobs.payloads import (
    GoogleNotificationJobPayload,
    MicrosoftNotificationJobPayload,
    SubscriptionRenewalJobPayload,
    WebhookDeliveryJobPayload,
)
from app.controllers.notifications.incoming_controller import (
    IncomingNotificationController,
)
from app.controllers.notifications.subscription_manager import SubscriptionManager
from app.controllers.webhooks.sender import WebhookSender
from app.exceptions import WebhookDeliveryError
from app.models.account import AccountProvider, AccountStatus
from app.models.job import Job, JobType
from app.repos.account import AccountRepo
from app.repos.job import JobRepo
from settings import settings

logger = logging.getLogger(__name__)


class JobProcessorController:
    """Dispatches and processes durable jobs from the jobs table."""

    def __init__(
        self,
        job_repo: JobRepo,
        incoming_notification_controller: IncomingNotificationController,
        subscription_manager: SubscriptionManager,
        account_repo: AccountRepo,
        webhook_sender: WebhookSender,
    ) -> None:
        self._job_repo = job_repo
        self._incoming_notification_controller = incoming_notification_controller
        self._subscription_manager = subscription_manager
        self._account_repo = account_repo
        self._webhook_sender = webhook_sender

    async def enqueue_google_notification(self, email_address: str, history_id: str) -> Job | None:
        normalized_email = email_address.lower()
        accounts = await self._account_repo.get_all_by_email_and_provider(normalized_email, AccountProvider.google)
        if not any(account.status == AccountStatus.active for account in accounts):
            logger.info(f"Skipping Google notification enqueue: no active account for {normalized_email}")
            return None
        payload = GoogleNotificationJobPayload(email_address=normalized_email, history_id=history_id)
        return await self._job_repo.enqueue(JobType.google_notification, payload.model_dump())

    async def enqueue_microsoft_notification(self, notification: dict[str, Any]) -> Job | None:
        subscription_id = str(notification.get("subscriptionId") or "").strip()
        if not subscription_id:
            logger.info("Skipping Microsoft notification enqueue: missing subscriptionId")
            return None

        account = await self._account_repo.get_by_subscription_id(subscription_id)
        if account is None or account.status != AccountStatus.active:
            logger.info(
                f"Skipping Microsoft notification enqueue: no active account for subscription {subscription_id}"
            )
            return None

        payload = MicrosoftNotificationJobPayload(notification=notification)
        return await self._job_repo.enqueue(JobType.microsoft_notification, payload.model_dump())

    async def enqueue_subscription_renewals(self, account_ids: list[int]) -> int:
        enqueued = 0
        for account_id in account_ids:
            payload = SubscriptionRenewalJobPayload(account_id=account_id)
            await self._job_repo.enqueue(JobType.subscription_renewal, payload.model_dump())
            enqueued += 1
        return enqueued

    async def enqueue_due_subscription_renewals(self, renew_within_seconds: int) -> int:
        account_ids = await self._account_repo.get_ids_needing_subscription_renewal(renew_within_seconds)
        if not account_ids:
            return 0
        return await self.enqueue_subscription_renewals(account_ids)

    async def enqueue_subscription_renewal_check(self, *, available_at: datetime | None = None) -> Job:
        """Enqueue the scheduler job that fans out due subscription renewals.

        Bootstrap by manually adding a single `subscription_renewal_check` job row.
        Each successful run enqueues the next run after the configured interval.
        """
        return await self._job_repo.enqueue(
            JobType.subscription_renewal_check, {}, max_attempts=5, available_at=available_at
        )

    async def process_available_jobs(self, worker_id: str, batch_size: int, lock_timeout_seconds: int) -> int:
        recovered = await self._job_repo.requeue_stale_processing(lock_timeout_seconds)
        if recovered:
            logger.warning(f"{worker_id}: recovered {recovered} stale job lock(s)")

        jobs = await self._job_repo.claim_batch(worker_id=worker_id, limit=batch_size)
        if not jobs:
            return 0

        for job in jobs:
            await self._process_one(job)
            # Release transaction-scoped advisory locks promptly (per account/job).
            await self._job_repo.commit()

        return len(jobs)

    async def _process_one(self, job: Job) -> None:
        try:
            await self._dispatch(job)
        except Exception as exc:
            retry_delay = self._retry_delay_seconds(job.attempts + 1)
            await self._job_repo.mark_retry_or_failed(job, str(exc), retry_delay_seconds=retry_delay)
            logger.warning(
                f"Job {job.id} ({job.type.name}) failed (attempt {job.attempts}/{job.max_attempts}): {exc}",
                exc_info=True,
            )
            return

        await self._job_repo.mark_completed(job)

    async def _dispatch(self, job: Job) -> None:
        if job.type == JobType.google_notification:
            google_payload = GoogleNotificationJobPayload.model_validate(job.payload)
            email_address = google_payload.email_address.strip().lower()
            history_id = google_payload.history_id.strip()
            if not email_address or not history_id:
                raise ValueError("google_notification payload cannot be blank")
            await self._incoming_notification_controller.process_google_notification(email_address, history_id)
            return

        if job.type == JobType.microsoft_notification:
            msft_payload = MicrosoftNotificationJobPayload.model_validate(job.payload)
            await self._incoming_notification_controller.process_microsoft_notification(msft_payload.notification)
            return

        if job.type == JobType.subscription_renewal_check:
            enqueued = await self.enqueue_due_subscription_renewals(
                settings.subscription_renewal.renew_within_hours * 3600
            )
            next_run_at = await self._job_repo.db_now() + timedelta(
                hours=settings.subscription_renewal.check_interval_hours
            )
            await self.enqueue_subscription_renewal_check(available_at=next_run_at)
            logger.info(
                f"subscription_renewal_check enqueued {enqueued} renewal job(s); next check at "
                f"{next_run_at.isoformat()}"
            )
            return

        if job.type == JobType.subscription_renewal:
            renewal_payload = SubscriptionRenewalJobPayload.model_validate(job.payload)
            account_id_raw = renewal_payload.account_id

            account = await self._account_repo.get(account_id_raw)
            if account is None:
                logger.warning(f"subscription_renewal account {account_id_raw} no longer exists; skipping")
                return
            if account.status != AccountStatus.active:
                logger.info(f"subscription_renewal account {account.email} is not active; skipping")
                return
            await self._subscription_manager.ensure_subscription(account)
            return

        if job.type == JobType.webhook_delivery:
            webhook_payload = WebhookDeliveryJobPayload.model_validate(job.payload)
            account_id_raw = webhook_payload.account_id

            account = await self._account_repo.get_by_id_with_app(account_id_raw)
            if account is None:
                logger.warning(f"webhook_delivery account {account_id_raw} no longer exists; skipping")
                return

            delivered = await self._webhook_sender.send_event(
                account=account,
                event_type=webhook_payload.event_type,
                object_data=webhook_payload.object_data,
                source=webhook_payload.source,
                email_id=webhook_payload.email_id,
                thread_id=webhook_payload.thread_id,
                max_retries=1,
            )
            if not delivered:
                raise WebhookDeliveryError(
                    f"Webhook delivery failed; {account.email=}, {webhook_payload.event_type=}, "
                    f"{webhook_payload.email_id=}, {webhook_payload.thread_id=}"
                )
            return

        raise ValueError(f"Unsupported job type: {job.type}")

    @staticmethod
    def _retry_delay_seconds(failure_count: int) -> int:
        # 5s, 10s, 20s, ... with a 5-minute ceiling.
        return min(300, max(5, 5 * (2 ** (failure_count - 1))))  # type: ignore
