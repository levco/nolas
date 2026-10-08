"""Redis grant capacity shared by all Nolas API and worker processes.

The Lua scripts are adapted from email-service's MicrosoftConcurrencyLimiter.
Nolas uses its own namespace because email-service can hold a lease while calling us.
"""

import asyncio
import hashlib
import logging
from contextlib import asynccontextmanager, contextmanager, suppress
from contextvars import ContextVar
from typing import AsyncGenerator, Generator
from uuid import uuid4

from redis.asyncio import Redis
from redis.exceptions import RedisError

from app.controllers.providers.exceptions import ProviderRateLimitError
from app.models.account import Account, AccountProvider

logger = logging.getLogger(__name__)
_worker_request = ContextVar("microsoft_worker_request", default=False)


@contextmanager
def microsoft_worker_requests() -> Generator[None, None, None]:
    token = _worker_request.set(True)
    try:
        yield
    finally:
        _worker_request.reset(token)


class MicrosoftConcurrencyLimiter:
    _ACQUIRE_SCRIPT = """
local redis_time = redis.call("TIME")
local now = (redis_time[1] * 1000) + math.floor(redis_time[2] / 1000)
local lease_expires_at = now + tonumber(ARGV[3])
local waiter_cutoff = now - tonumber(ARGV[4])

redis.call("ZREMRANGEBYSCORE", KEYS[1], "-inf", now)
redis.call("ZREMRANGEBYSCORE", KEYS[2], "-inf", now)
redis.call("ZREMRANGEBYSCORE", KEYS[3], "-inf", waiter_cutoff)
redis.call("ZREMRANGEBYSCORE", KEYS[4], "-inf", waiter_cutoff)

local priority = ARGV[2]
local waiter_key = KEYS[4]
local holder_key = KEYS[2]
if priority == "mailbox" then
    waiter_key = KEYS[3]
    holder_key = KEYS[1]
end
redis.call("ZADD", waiter_key, "NX", now, ARGV[1])

local total_active = redis.call("ZCARD", KEYS[1]) + redis.call("ZCARD", KEYS[2])
if total_active >= tonumber(ARGV[5]) then
    return 0
end

local queue_head = redis.call("ZRANGE", waiter_key, 0, 0)[1]
if queue_head ~= ARGV[1] then
    return 0
end

if priority == "worker" then
    if redis.call("ZCARD", KEYS[3]) > 0 then
        return 0
    end
    if redis.call("ZCARD", KEYS[2]) >= tonumber(ARGV[6]) then
        return 0
    end
end

redis.call("ZREM", waiter_key, ARGV[1])
redis.call("ZADD", holder_key, lease_expires_at, ARGV[1])
local key_ttl = math.max(tonumber(ARGV[3]), tonumber(ARGV[4])) * 2
for index = 1, 4 do
    redis.call("PEXPIRE", KEYS[index], key_ttl)
end
return 1
"""

    _RELEASE_SCRIPT = """
redis.call("ZREM", KEYS[1], ARGV[1])
redis.call("ZREM", KEYS[2], ARGV[1])
redis.call("ZREM", KEYS[3], ARGV[1])
redis.call("ZREM", KEYS[4], ARGV[1])
return 1
"""

    _RENEW_SCRIPT = """
local redis_time = redis.call("TIME")
local now = (redis_time[1] * 1000) + math.floor(redis_time[2] / 1000)
local lease_expires_at = now + tonumber(ARGV[2])
local holder_key_ttl = tonumber(ARGV[2]) * 2
if redis.call("ZSCORE", KEYS[1], ARGV[1]) then
    redis.call("ZADD", KEYS[1], lease_expires_at, ARGV[1])
    redis.call("PEXPIRE", KEYS[1], holder_key_ttl)
    return 1
end
if redis.call("ZSCORE", KEYS[2], ARGV[1]) then
    redis.call("ZADD", KEYS[2], lease_expires_at, ARGV[1])
    redis.call("PEXPIRE", KEYS[2], holder_key_ttl)
    return 1
end
return 0
"""

    def __init__(
        self,
        redis: Redis,
        total_limit: int,
        worker_limit: int,
        lease_seconds: int,
        acquire_timeout_seconds: int,
    ) -> None:
        if not 1 <= total_limit <= 4 or not 0 <= worker_limit < total_limit:
            raise ValueError("Total limit must be 1–4 and worker limit must be non-negative and lower than total")
        if lease_seconds < 1 or acquire_timeout_seconds < 1:
            raise ValueError("Lease and acquisition timeout must be positive")
        self._redis = redis
        self._total_limit = total_limit
        self._worker_limit = worker_limit
        self._lease_ms = lease_seconds * 1000
        self._acquire_timeout = acquire_timeout_seconds
        self._waiter_ttl_ms = max(acquire_timeout_seconds * 2, lease_seconds) * 1000

    def _keys(self, account: Account) -> tuple[str, str, str, str]:
        grant_hash = hashlib.sha256(str(account.uuid).encode()).hexdigest()
        prefix = f"nolas:microsoft-concurrency:{{{grant_hash}}}"
        return (
            f"{prefix}:mailbox-holders",
            f"{prefix}:worker-holders",
            f"{prefix}:mailbox-waiters",
            f"{prefix}:worker-waiters",
        )

    @asynccontextmanager
    async def acquire(self, account: Account) -> AsyncGenerator[None, None]:
        if account.provider != AccountProvider.microsoft:
            yield
            return

        keys = self._keys(account)
        priority = "worker" if _worker_request.get() else "mailbox"
        token = f"{priority}:{uuid4()}"
        renewal: asyncio.Task[None] | None = None

        async def renew() -> None:
            while True:
                await asyncio.sleep(self._lease_ms / 3000)
                try:
                    async with asyncio.timeout(self._lease_ms / 3000):
                        renewed = await self._redis.eval(self._RENEW_SCRIPT, 2, *keys[:2], token, self._lease_ms)
                except (RedisError, TimeoutError):
                    logger.warning("Could not renew Microsoft concurrency lease; request will continue", exc_info=True)
                    continue
                if renewed != 1:
                    logger.error("Lost Microsoft concurrency lease; request will continue")
                    return

        try:
            try:
                async with asyncio.timeout(self._acquire_timeout):
                    while not await self._redis.eval(
                        self._ACQUIRE_SCRIPT,
                        4,
                        *keys,
                        token,
                        priority,
                        self._lease_ms,
                        self._waiter_ttl_ms,
                        self._total_limit,
                        self._worker_limit,
                    ):
                        await asyncio.sleep(0.05)
            except TimeoutError as exc:
                raise ProviderRateLimitError("Timed out waiting for Microsoft grant concurrency capacity.") from exc
            except RedisError:
                logger.warning("Redis unavailable; bypassing Microsoft grant concurrency limiter", exc_info=True)
            else:
                renewal = asyncio.create_task(renew(), name="microsoft-lease-renewal")
            yield
        finally:
            if renewal is not None:
                renewal.cancel()
                with suppress(asyncio.CancelledError):
                    await renewal
            # Also removes waiters on timeout/cancellation. A crashed process's entries expire.
            try:
                await self._redis.eval(self._RELEASE_SCRIPT, 4, *keys, token)
            except RedisError:
                logger.warning("Could not release Microsoft concurrency lease; it will expire", exc_info=True)
