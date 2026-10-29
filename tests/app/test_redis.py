from unittest.mock import AsyncMock

import pytest
from redis.asyncio.connection import SSLConnection
from redis.asyncio.sentinel import SentinelConnectionPool

from app.init_redis_utils import close_redis_instance, get_redis_instance
from settings.settings import RedisSettings


@pytest.mark.asyncio
async def test_direct_redis_preserves_url_and_timeouts() -> None:
    client = get_redis_instance(
        RedisSettings(REDIS_URL="rediss://user:secret@redis.example:6380/2", REDIS_SOCKET_TIMEOUT_SECONDS=7),
        use_sentinel=False,
    )
    try:
        pool = client.connection_pool
        assert not isinstance(pool, SentinelConnectionPool)
        assert pool.connection_class is SSLConnection
        assert (
            pool.connection_kwargs.items()
            >= {
                "host": "redis.example",
                "port": 6380,
                "username": "user",
                "password": "secret",
                "db": 2,
                "socket_timeout": 7,
                "socket_connect_timeout": 7,
            }.items()
        )
    finally:
        await close_redis_instance(client)


@pytest.mark.asyncio
async def test_sentinel_uses_master_discovery_and_separate_credentials() -> None:
    client = get_redis_instance(
        RedisSettings(
            REDIS_URL="redis://ignored:6379/2",
            SENTINEL_HOST="sentinel.example",
            SENTINEL_PORT=26379,
            SENTINEL_MASTER_NAME="nolas-master",
            SENTINEL_SOCKET_TIMEOUT=5,
            SENTINEL_PASSWORD="sentinel-secret",
            REDIS_PASSWORD="redis-secret",
        ),
        use_sentinel=True,
    )
    try:
        pool = client.connection_pool
        assert isinstance(pool, SentinelConnectionPool)
        assert pool.is_master
        assert pool.service_name == "nolas-master"
        assert (
            pool.connection_kwargs.items()
            >= {
                "password": "redis-secret",
                "socket_timeout": 5,
                "socket_connect_timeout": 5,
            }.items()
        )
        assert pool.connection_kwargs.get("db", 0) == 0
        discovery = pool.sentinel_manager.sentinels[0]
        assert (
            discovery.connection_pool.connection_kwargs.items()
            >= {
                "host": "sentinel.example",
                "port": 26379,
                "password": "sentinel-secret",
                "socket_timeout": 5,
                "socket_connect_timeout": 5,
            }.items()
        )
    finally:
        await close_redis_instance(client)


@pytest.mark.asyncio
@pytest.mark.parametrize("master_close_fails", [False, True])
async def test_shutdown_closes_discovery_connections(monkeypatch: pytest.MonkeyPatch, master_close_fails: bool) -> None:
    client = get_redis_instance(RedisSettings(), use_sentinel=True)
    discovery = client.connection_pool.sentinel_manager.sentinels[0]
    master_close = AsyncMock(side_effect=RuntimeError("close failed") if master_close_fails else None)
    discovery_close = AsyncMock()
    monkeypatch.setattr(client, "aclose", master_close)
    monkeypatch.setattr(discovery, "aclose", discovery_close)
    if master_close_fails:
        with pytest.raises(RuntimeError, match="close failed"):
            await close_redis_instance(client)
    else:
        await close_redis_instance(client)
    master_close.assert_awaited_once_with()
    discovery_close.assert_awaited_once_with()
