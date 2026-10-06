import asyncio
import os
from contextlib import AsyncExitStack
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
import pytest_asyncio
from redis.asyncio import Redis
from redis.exceptions import ConnectionError

from app.controllers.providers.exceptions import ProviderError, ProviderRateLimitError
from app.controllers.providers.microsoft.concurrency_limiter import (
    MicrosoftConcurrencyLimiter,
    microsoft_worker_requests,
)
from app.models.account import Account, AccountProvider


def _account() -> Account:
    return Account(email=f"{uuid4()}@example.com", provider=AccountProvider.microsoft)


def _limiter(redis: Redis, total: int = 4, workers: int = 3) -> MicrosoftConcurrencyLimiter:
    return MicrosoftConcurrencyLimiter(redis, total, workers, lease_seconds=1, acquire_timeout_seconds=1)


@pytest_asyncio.fixture
async def mailbox():
    url = os.getenv("REDIS_TEST_URL")
    if not url:
        pytest.skip("Set REDIS_TEST_URL to run the Redis Lua integration checks")
    redis = Redis.from_url(url, socket_timeout=1, socket_connect_timeout=1)
    account = _account()
    limiter = _limiter(redis)
    try:
        await redis.ping()
        yield redis, account
    finally:
        await redis.delete(*limiter._keys(account))
        await redis.aclose()


@pytest.mark.asyncio
async def test_replicas_share_mailbox_capacity_and_reserve_api_slot(mailbox) -> None:
    redis, account = mailbox
    first, second = _limiter(redis), _limiter(redis)
    same_mailbox = Account(email=account.email.upper(), provider=AccountProvider.microsoft)
    acquired = asyncio.Event()
    release = asyncio.Event()

    async def waiting_worker() -> None:
        with microsoft_worker_requests():
            async with second.acquire(same_mailbox):
                acquired.set()
                await release.wait()

    async with AsyncExitStack() as stack:
        with microsoft_worker_requests():
            await stack.enter_async_context(first.acquire(account))
            await stack.enter_async_context(second.acquire(account))
            await stack.enter_async_context(first.acquire(account))
        waiter = asyncio.create_task(waiting_worker())
        await asyncio.sleep(0.1)
        assert not acquired.is_set()
        # Fourth slot is still available for API traffic despite a waiting worker.
        async with second.acquire(same_mailbox):
            assert await redis.zcard(first._keys(account)[0]) == 1
            assert await redis.zcard(first._keys(account)[1]) == 3
        assert not acquired.is_set()
    await asyncio.wait_for(acquired.wait(), 1)
    release.set()
    await waiter
    assert not any([await redis.exists(key) for key in first._keys(account)])


@pytest.mark.asyncio
async def test_timeout_and_cancellation_remove_waiters(mailbox) -> None:
    redis, account = mailbox
    limiter = _limiter(redis, total=1, workers=0)
    async with limiter.acquire(account):
        with pytest.raises(ProviderRateLimitError):
            async with limiter.acquire(account):
                pytest.fail("capacity exceeded")
        assert await redis.zcard(limiter._keys(account)[2]) == 0

        async def wait() -> None:
            async with limiter.acquire(account):
                pytest.fail("capacity exceeded")

        task = asyncio.create_task(wait())
        await asyncio.sleep(0.1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert await redis.zcard(limiter._keys(account)[2]) == 0


@pytest.mark.asyncio
async def test_renewal_and_expired_holder_recovery(mailbox) -> None:
    redis, account = mailbox
    limiter = _limiter(redis, total=1, workers=0)
    async with limiter.acquire(account):
        await asyncio.sleep(1.2)
        assert await redis.zcard(limiter._keys(account)[0]) == 1
        with pytest.raises(ProviderRateLimitError):
            async with _limiter(redis, total=1, workers=0).acquire(account):
                pytest.fail("renewed holder was evicted")
    # Simulate a process that died without releasing its expired holder/waiter.
    await redis.zadd(limiter._keys(account)[0], {"dead-holder": 0})
    await redis.zadd(limiter._keys(account)[2], {"dead-waiter": 0})
    async with limiter.acquire(account):
        assert await redis.zcard(limiter._keys(account)[0]) == 1
        assert await redis.zcard(limiter._keys(account)[2]) == 0


@pytest.mark.asyncio
async def test_waiting_api_requests_take_priority_over_workers(mailbox) -> None:
    redis, account = mailbox
    limiter = _limiter(redis, total=2, workers=1)
    order = []

    async def request(name, worker=False) -> None:
        if worker:
            with microsoft_worker_requests():
                async with limiter.acquire(account):
                    order.append(name)
        else:
            async with limiter.acquire(account):
                order.append(name)
                await asyncio.sleep(0.05)

    async with AsyncExitStack() as stack:
        await stack.enter_async_context(limiter.acquire(account))
        await stack.enter_async_context(limiter.acquire(account))
        worker = asyncio.create_task(request("worker", worker=True))
        await asyncio.sleep(0.06)
        api_first = asyncio.create_task(request("api-first"))
        await asyncio.sleep(0.06)
        api_second = asyncio.create_task(request("api-second"))
        await asyncio.sleep(0.06)
    await asyncio.gather(worker, api_first, api_second)
    assert order == ["api-first", "api-second", "worker"]


@pytest.mark.asyncio
async def test_exception_and_holder_cancellation_release_capacity(mailbox) -> None:
    redis, account = mailbox
    limiter = _limiter(redis)
    with pytest.raises(ValueError):
        async with limiter.acquire(account):
            raise ValueError("request failed")
    entered = asyncio.Event()

    async def request() -> None:
        async with limiter.acquire(account):
            entered.set()
            await asyncio.Event().wait()

    task = asyncio.create_task(request())
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not any([await redis.exists(key) for key in limiter._keys(account)])


@pytest.mark.asyncio
async def test_other_mailboxes_are_independent(mailbox) -> None:
    redis, account = mailbox
    limiter = _limiter(redis, total=1, workers=0)
    other = _account()
    async with limiter.acquire(account), limiter.acquire(other):
        assert await redis.zcard(limiter._keys(account)[0]) == 1
        assert await redis.zcard(limiter._keys(other)[0]) == 1


@pytest.mark.asyncio
async def test_redis_failure_does_not_bypass_limit() -> None:
    redis = AsyncMock()
    redis.eval.side_effect = ConnectionError("offline")
    with pytest.raises(ProviderError) as error:
        async with _limiter(redis).acquire(_account()):
            pytest.fail("request must not proceed without Redis")
    assert error.value.status_code == 503


@pytest.mark.asyncio
async def test_lease_loss_cancels_active_request() -> None:
    redis = AsyncMock()
    redis.eval.side_effect = [1, 0, 1]
    with pytest.raises(ProviderError) as error:
        async with _limiter(redis).acquire(_account()):
            await asyncio.sleep(2)
    assert error.value.status_code == 503
    assert redis.eval.await_count == 3


@pytest.mark.asyncio
async def test_google_bypasses_redis_and_priority_resets() -> None:
    redis = AsyncMock()
    account = SimpleNamespace(provider=AccountProvider.google)
    with microsoft_worker_requests():
        async with _limiter(redis).acquire(account):
            pass
    redis.eval.assert_not_awaited()


@pytest.mark.parametrize("total,workers", [(0, 0), (3, 3), (3, -1), (5, 3)])
def test_invalid_limits_are_rejected(total, workers) -> None:
    with pytest.raises(ValueError):
        _limiter(AsyncMock(), total=total, workers=workers)
