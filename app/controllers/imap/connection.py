import asyncio
import logging
import time

from aioimaplib import IMAP4_SSL

from app.models import Account
from settings import settings

logger = logging.getLogger(__name__)


class RateLimiter:
    """Token bucket rate limiter for IMAP connections."""

    def __init__(self, rate: float, burst: int | None = None):
        self._logger = logging.getLogger(__name__)
        self._rate = rate  # tokens per second
        self._burst = burst or int(rate * 2)  # burst capacity
        self._tokens = self._burst
        self._last_update = time.time()
        self._lock = asyncio.Lock()

    async def acquire(self, tokens: int = 1) -> None:
        """Acquire tokens from the bucket, waiting if necessary."""
        async with self._lock:
            now = time.time()
            elapsed = now - self._last_update

            # Add tokens based on elapsed time
            self._tokens = min(self._burst, int(self._tokens + elapsed * self._rate))
            self._last_update = now

            if self._tokens >= tokens:
                self._tokens -= tokens
                return

            # Calculate wait time
            wait_time = (tokens - self._tokens) / self._rate
            await asyncio.sleep(wait_time)
            self._tokens = 0


class ConnectionManager:
    """Manages IMAP connections with pooling and rate limiting."""

    def __init__(self) -> None:
        self._logger = logging.getLogger(__name__)
        self._rate_limiters: dict[str, RateLimiter] = {}
        self._connection_locks: dict[str, asyncio.Semaphore] = {}
        self._lock = asyncio.Lock()

        # Simple connection limit per provider - no need for complex pooling with polling
        connection_limit = 10

        # TODO: Make dynamic.
        IMAP_HOSTS = ["imap.purelymail.com"]

        for imap_host in IMAP_HOSTS:
            self._connection_locks[imap_host] = asyncio.Semaphore(connection_limit)
            self._rate_limiters[imap_host] = RateLimiter(rate=connection_limit - 1, burst=connection_limit)

    async def get_connection_or_fail(self, account: Account, folder: str | None = None) -> IMAP4_SSL:
        """Get an IMAP connection for the account."""
        connection = await self.get_connection(account, folder)
        if not connection:
            raise ValueError("Failed to get IMAP connection")
        return connection

    async def get_connection(self, account: Account, folder: str | None = None) -> IMAP4_SSL | None:
        """Get an IMAP connection for the account."""
        imap_provider = account.provider_context.get("imap_host")
        if not imap_provider:
            raise ValueError("IMAP provider not found in account context")

        # Rate limiting
        if imap_provider in self._rate_limiters:
            await self._rate_limiters[imap_provider].acquire()

        # Create new connection (no connection reuse needed for polling)
        async with self._connection_locks.get(imap_provider, asyncio.Semaphore(5)):
            return await self._create_new_connection(account, folder)

    async def _create_new_connection(self, account: Account, folder: str | None = None) -> IMAP4_SSL | None:
        """Create a new IMAP connection."""
        imap_host = account.provider_context.get("imap_host")
        if not imap_host:
            raise ValueError("IMAP host not found in account context")

        try:
            # Use async IMAP library
            connection = IMAP4_SSL(host=imap_host, port=993, timeout=settings.imap.timeout)
            await connection.wait_hello_from_server()
            response = await connection.login(account.email, account.credentials)
            if response.result != "OK":
                self._logger.warning(f"Failed to login to {imap_host} for {account.email}: {response.result}")
                return None

            if folder:
                await connection.select(folder)

            self._logger.debug(f"Created new IMAP connection for {account.email}:{folder}")
            return connection

        except Exception as e:
            self._logger.error(f"Failed to create IMAP connection for {account.email}: {e}")
            raise

    async def close_connection(self, connection: IMAP4_SSL, account: Account) -> None:
        """Close an IMAP connection."""
        try:
            await asyncio.wait_for(connection.logout(), timeout=5)
            self._logger.debug(f"Closed connection for {account.email}")
        except asyncio.TimeoutError:
            self._logger.warning(f"Timeout closing connection for {account.email}, forcing close")
            # Force close the connection if logout hangs
            try:
                connection.close()
            except Exception:
                pass
        except Exception as e:
            self._logger.warning(f"Error closing connection for {account.email}: {e}")

    async def close_all_connections(self) -> None:
        """Close all connections - simplified since we don't track persistent connections."""
        self._logger.info("Connection manager cleanup complete (no persistent connections to close)")
