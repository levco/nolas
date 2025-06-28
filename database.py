import asyncio
import logging

import asyncpg
from asyncpg import Pool

from settings import settings

logger = logging.getLogger(__name__)


class DatabaseManager:
    """Async PostgreSQL database manager with connection pooling."""

    def __init__(self) -> None:
        self._pool: Pool | None = None
        self._lock = asyncio.Lock()

    async def init_pool(self, database_url: str | None = None) -> None:
        """Initialize the connection pool."""
        if self._pool is not None:
            return

        async with self._lock:
            if self._pool is not None:
                return

            db_url = database_url or f"{settings.database.host}/{settings.database.name}"
            min_pool_size = settings.database.min_pool_size
            max_pool_size = settings.database.max_pool_size

            try:
                self._pool = await asyncpg.create_pool(
                    db_url,
                    min_size=min_pool_size,
                    max_size=max_pool_size,
                    command_timeout=60,
                    server_settings={
                        "application_name": "nolas",
                        "jit": "off",  # Disable JIT for better connection startup time
                    },
                )
                logger.info(f"Database pool initialized with {min_pool_size}-{max_pool_size} connections")

            except Exception as e:
                logger.error(f"Failed to initialize database pool: {e}")
                raise

    async def close_pool(self) -> None:
        """Close the connection pool."""
        if self._pool:
            await self._pool.close()
            self._pool = None
            logger.info("Database pool closed")
