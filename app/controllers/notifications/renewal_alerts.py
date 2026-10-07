import asyncio
import logging
import time

from lev.services.slack import Slack
from redis.asyncio import Redis

from app.models.job import Job
from settings import settings

logger = logging.getLogger(__name__)


class RenewalAlerts:
    def __init__(self, redis: Redis, slack: Slack) -> None:
        self._redis = redis
        self._slack = slack

    async def record_failure(self, job: Job) -> None:
        config = settings.slack
        if not config.bot_token or not config.renewal_alert_channel_id:
            return

        # Shared across worker loops/replicas; job IDs prevent counting a failure twice.
        key = f"nolas:{settings.environment.value}:renewal-failures"
        try:
            now = time.time()
            async with self._redis.pipeline(transaction=True) as pipe:
                pipe.zadd(key, {str(job.id): now}, nx=True)
                pipe.zremrangebyscore(key, "-inf", now - config.renewal_failure_window_seconds)
                pipe.zcard(key)
                pipe.expire(key, config.renewal_failure_window_seconds)
                results = await pipe.execute()
            count = int(results[2])
            if count < config.renewal_failure_threshold:
                return

            claimed = await self._redis.set(f"{key}:cooldown", "1", nx=True, ex=config.renewal_alert_cooldown_seconds)
            if not claimed:
                return

            # The common Slack client is synchronous; keep its I/O off the event loop.
            await asyncio.to_thread(
                self._slack.post_message_to_channel,
                config.renewal_alert_channel_id,
                f"Nolas ({settings.environment.value}): {count} subscription renewal jobs exhausted their retries "
                f"in the last {config.renewal_failure_window_seconds} seconds. "
                f"Latest: job {job.id}, account {job.payload.get('account_id')}, "
                f"attempts {job.attempts}/{job.max_attempts}. Check worker logs and jobs.last_error for details.",
            )
        except Exception:
            # Alerting must never interrupt job processing. Failed sends keep the cooldown
            # to avoid hammering Slack during an outage; a later failure can alert again.
            logger.exception("Failed to send subscription renewal alert for job %s", job.id)
