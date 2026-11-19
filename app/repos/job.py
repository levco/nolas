from datetime import datetime, timedelta
from typing import Any

import sqlalchemy as sa
from sqlalchemy import update

from app.models.job import Job, JobStatus, JobType
from app.repos.base import BaseRepo

# Provider rate limits do not use the failure budget, but they cannot retry forever.
MAX_RATE_LIMIT_RETRIES = 10


class JobRepo(BaseRepo[Job]):
    """Repository for durable background jobs."""

    def __init__(self) -> None:
        super().__init__(Job)

    async def enqueue(
        self,
        type: JobType,
        payload: dict[str, Any],
        *,
        max_attempts: int = 5,
        available_at: datetime | None = None,
    ) -> Job:
        effective_available_at = available_at or await self.db_now()
        job = Job(
            type=type,
            status=JobStatus.pending,
            payload=payload,
            max_attempts=max_attempts,
            available_at=effective_available_at,
        )
        await self.add(job)
        return job

    async def get_pending_google_notification(self, email_address: str) -> Job | None:
        stmt = (
            self.base_stmt.where(
                Job.type == JobType.google_notification,
                Job.status == JobStatus.pending,
                Job.payload["email_address"].astext == email_address,
            )
            .order_by(Job.id.asc())
            .limit(1)
            .with_for_update(skip_locked=True)
        )
        return (await self.execute(stmt)).first()

    async def claim_batch(self, worker_id: str, limit: int) -> list[Job]:
        claimable = (
            sa.select(Job.id)
            .where(Job.status == JobStatus.pending, Job.available_at <= sa.func.now())
            .order_by(Job.available_at.asc(), Job.id.asc())
            .limit(limit)
            .with_for_update(skip_locked=True)
            .cte("claimable_jobs")
        )
        claim_stmt = (
            update(Job)
            .where(Job.id.in_(sa.select(claimable.c.id)))
            .values(
                status=JobStatus.processing,
                locked_by=worker_id,
                locked_at=sa.func.now(),
                completed_at=None,
                last_error=None,
            )
            .returning(Job)
        )
        result = await self._db.session.execute(claim_stmt)
        jobs = list(result.scalars().all())
        jobs.sort(key=lambda job: (job.available_at, job.id))
        return jobs

    async def mark_completed(self, job: Job) -> None:
        db_now = await self.db_now()
        job.status = JobStatus.completed
        job.completed_at = db_now
        job.locked_at = None
        job.locked_by = None
        await self.flush()

    async def mark_retry_or_failed(
        self, job: Job, error: str, retry_delay_seconds: int, *, rate_limited: bool = False
    ) -> None:
        if rate_limited:
            job.rate_limit_retries += 1
        else:
            job.attempts += 1
        job.last_error = error[:4000]
        job.completed_at = None
        job.locked_at = None
        job.locked_by = None

        if job.attempts >= job.max_attempts or job.rate_limit_retries >= MAX_RATE_LIMIT_RETRIES:
            job.status = JobStatus.failed
            await self.flush()
            return

        db_now = await self.db_now()
        job.status = JobStatus.pending
        job.available_at = db_now + timedelta(seconds=max(1, retry_delay_seconds))
        await self.flush()

    async def requeue_stale_processing(self, lock_timeout_seconds: int) -> int:
        db_now = await self.db_now()
        timeout = db_now - timedelta(seconds=lock_timeout_seconds)
        stmt = (
            update(Job)
            .where(Job.status == JobStatus.processing, Job.locked_at.is_not(None), Job.locked_at < timeout)
            .values(
                status=JobStatus.pending,
                completed_at=None,
                locked_at=None,
                locked_by=None,
                available_at=sa.func.now(),
                last_error="Recovered stale processing lock",
            )
        )
        result = await self._db.session.execute(stmt)
        await self.flush()
        return int(result.rowcount or 0)
