import asyncio
import logging
import time
from dataclasses import dataclass
from typing import Any

from aioimaplib import IMAP4_SSL

from models import AccountConfig, Config
from settings import settings

logger = logging.getLogger(__name__)


@dataclass
class ConnectionInfo:
    connection: IMAP4_SSL
    account_email: str
    provider: str
    last_used: float
    is_idle: bool = False
    selected_folder: str | None = None


class RateLimiter:
    """Token bucket rate limiter for IMAP connections."""

    def __init__(self, rate: float, burst: int | None = None):
        self.rate = rate  # tokens per second
        self.burst = burst or int(rate * 2)  # burst capacity
        self.tokens = self.burst
        self.last_update = time.time()
        self._lock = asyncio.Lock()

    async def acquire(self, tokens: int = 1) -> None:
        """Acquire tokens from the bucket, waiting if necessary."""
        async with self._lock:
            now = time.time()
            elapsed = now - self.last_update

            # Add tokens based on elapsed time
            self.tokens = min(self.burst, int(self.tokens + elapsed * self.rate))
            self.last_update = now

            if self.tokens >= tokens:
                self.tokens -= tokens
                return

            # Calculate wait time
            wait_time = (tokens - self.tokens) / self.rate
            await asyncio.sleep(wait_time)
            self.tokens = 0


class ConnectionManager:
    """Manages IMAP connections with pooling and rate limiting."""

    def __init__(self) -> None:
        self.connections: dict[str, list[ConnectionInfo]] = {}  # provider -> connections
        self.rate_limiters: dict[str, RateLimiter] = {}
        self.connection_locks: dict[str, asyncio.Semaphore] = {}
        self._lock = asyncio.Lock()

        # Initialize rate limiters and connection limits
        for provider in Config.IMAP_SERVERS.keys():
            limit = Config.CONNECTION_LIMITS.get(provider, 10)
            self.connection_locks[provider] = asyncio.Semaphore(limit)
            self.rate_limiters[provider] = RateLimiter(rate=Config.RATE_LIMIT_PER_PROVIDER, burst=limit)

    async def get_connection(self, account: AccountConfig, folder: str | None = None) -> IMAP4_SSL:
        """Get an IMAP connection for the account, reusing if possible."""
        provider = account.provider

        # Rate limiting
        await self.rate_limiters[provider].acquire()

        # Try to reuse existing connection
        connection_info = await self._find_reusable_connection(account, folder)
        if connection_info:
            return connection_info.connection

        # Create new connection if under limit
        async with self.connection_locks[provider]:
            return await self._create_new_connection(account, folder)

    async def _find_reusable_connection(
        self, account: AccountConfig, folder: str | None = None
    ) -> ConnectionInfo | None:
        """Find a reusable connection for the account."""
        provider = account.provider

        if provider not in self.connections:
            return None

        async with self._lock:
            for conn_info in self.connections[provider]:
                if (
                    conn_info.account_email == account.email
                    and not conn_info.is_idle
                    and (folder is None or conn_info.selected_folder == folder)
                ):
                    # Check if connection is still alive
                    if await self._is_connection_alive(conn_info.connection):
                        conn_info.last_used = time.time()
                        if folder and conn_info.selected_folder != folder:
                            await self._select_folder(conn_info.connection, folder)
                            conn_info.selected_folder = folder
                        return conn_info
                    else:
                        # Remove dead connection
                        self.connections[provider].remove(conn_info)

        return None

    async def _create_new_connection(self, account: AccountConfig, folder: str | None = None) -> Any:
        """Create a new IMAP connection."""
        provider = account.provider
        server_host = Config.IMAP_SERVERS[provider]

        try:
            # Use async IMAP library
            connection = IMAP4_SSL(host=server_host, port=993, timeout=settings.imap.timeout)
            await connection.wait_hello_from_server()
            await connection.login(account.username, account.password)

            if folder:
                await connection.select(folder)

            # Store connection info
            conn_info = ConnectionInfo(
                connection=connection,
                account_email=account.email,
                provider=provider,
                last_used=time.time(),
                selected_folder=folder,
            )

            async with self._lock:
                if provider not in self.connections:
                    self.connections[provider] = []
                self.connections[provider].append(conn_info)

            logger.info(f"Created new IMAP connection for {account.email}:{folder}")
            return connection

        except Exception as e:
            logger.error(f"Failed to create IMAP connection for {account.email}: {e}")
            raise

    async def _is_connection_alive(self, connection: IMAP4_SSL) -> bool:
        """Check if an IMAP connection is still alive."""
        try:
            # Send NOOP command to check connection
            await asyncio.wait_for(connection.noop(), timeout=5)
            return True
        except Exception:
            return False

    async def _select_folder(self, connection: IMAP4_SSL, folder: str) -> None:
        """Select a folder on the IMAP connection."""
        await connection.select(folder)

    async def start_idle(self, connection: IMAP4_SSL, account_email: str) -> None:
        """Start IDLE mode on a connection."""
        provider = self._get_provider_by_email(account_email)
        if not provider:
            return

        async with self._lock:
            for conn_info in self.connections.get(provider, []):
                if conn_info.connection == connection:
                    conn_info.is_idle = True
                    break

        await connection.idle_start()

    async def stop_idle(self, connection: IMAP4_SSL, account_email: str) -> None:
        """Stop IDLE mode on a connection."""
        provider = self._get_provider_by_email(account_email)
        if not provider:
            return

        async with self._lock:
            for conn_info in self.connections.get(provider, []):
                if conn_info.connection == connection:
                    conn_info.is_idle = False
                    break

        await connection.idle_done()

    async def release_connection(self, connection: IMAP4_SSL, account_email: str) -> None:
        """Release a connection back to the pool."""
        provider = self._get_provider_by_email(account_email)
        if not provider:
            return

        async with self._lock:
            for conn_info in self.connections.get(provider, []):
                if conn_info.connection == connection:
                    conn_info.is_idle = False
                    conn_info.last_used = time.time()
                    break

    async def close_connection(self, connection: IMAP4_SSL, account_email: str) -> None:
        """Close and remove a connection from the pool."""
        provider = self._get_provider_by_email(account_email)
        if not provider:
            return

        async with self._lock:
            for conn_info in self.connections.get(provider, []):
                if conn_info.connection == connection:
                    self.connections[provider].remove(conn_info)
                    break

        try:
            await connection.logout()
        except Exception as e:
            logger.warning(f"Error closing connection for {account_email}: {e}")

    def _get_provider_by_email(self, account_email: str) -> str | None:
        """Get provider for an account email."""
        domain = account_email.split("@")[1].lower()
        for provider in Config.IMAP_SERVERS.keys():
            if provider in domain or domain in provider:
                return provider
        return None

    async def cleanup_idle_connections(self, max_idle_time: int = 600) -> None:
        """Clean up connections that have been idle too long."""
        current_time = time.time()
        connections_to_close = []

        async with self._lock:
            for provider, conn_list in self.connections.items():
                for conn_info in conn_list[:]:  # Copy list to avoid modification during iteration
                    if current_time - conn_info.last_used > max_idle_time:
                        connections_to_close.append((conn_info.connection, conn_info.account_email))
                        conn_list.remove(conn_info)

        # Close idle connections
        for connection, account_email in connections_to_close:
            try:
                await self.close_connection(connection, account_email)
                logger.info(f"Closed idle connection for {account_email}")
            except Exception as e:
                logger.warning(f"Error closing idle connection for {account_email}: {e}")

    async def get_connection_stats(self) -> dict[str, dict[str, int]]:
        """Get statistics about current connections."""
        stats = {}

        async with self._lock:
            for provider, conn_list in self.connections.items():
                stats[provider] = {
                    "total": len(conn_list),
                    "idle": sum(1 for conn in conn_list if conn.is_idle),
                    "active": sum(1 for conn in conn_list if not conn.is_idle),
                }

        return stats

    async def close_all_connections(self) -> None:
        """Close all connections in the pool."""
        connections_to_close = []

        async with self._lock:
            for provider, conn_list in self.connections.items():
                for conn_info in conn_list:
                    connections_to_close.append((conn_info.connection, conn_info.account_email))
                conn_list.clear()

        # Close all connections
        for connection, account_email in connections_to_close:
            try:
                await self.close_connection(connection, account_email)
            except Exception as e:
                logger.warning(f"Error closing connection for {account_email}: {e}")

        logger.info("All IMAP connections closed")
