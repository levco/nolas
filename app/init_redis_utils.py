import asyncio

from redis.asyncio import Redis
from redis.asyncio.sentinel import Sentinel, SentinelConnectionPool

from settings.settings import RedisSettings


def get_redis_instance(config: RedisSettings, use_sentinel: bool) -> Redis:
    if use_sentinel:
        return Sentinel(
            [(config.sentinel_host, config.sentinel_port)],
            sentinel_kwargs={
                "password": config.sentinel_password,
                "socket_timeout": config.sentinel_socket_timeout,
                "socket_connect_timeout": config.sentinel_socket_timeout,
            },
            password=config.redis_password,
            socket_timeout=config.sentinel_socket_timeout,
            socket_connect_timeout=config.sentinel_socket_timeout,
        ).master_for(config.sentinel_master_name)
    return Redis.from_url(
        config.url,
        socket_timeout=config.socket_timeout_seconds,
        socket_connect_timeout=config.socket_timeout_seconds,
    )


async def close_redis_instance(client: Redis) -> None:
    try:
        await client.aclose()
    finally:
        # The master client owns its pool, but Sentinel discovery has separate connections.
        if isinstance(client.connection_pool, SentinelConnectionPool):
            await asyncio.gather(*(sentinel.aclose() for sentinel in client.connection_pool.sentinel_manager.sentinels))
