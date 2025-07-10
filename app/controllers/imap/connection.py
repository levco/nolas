import asyncio
import logging
import time
from dataclasses import dataclass

from aioimaplib import IMAP4_SSL

from app.models import Account
from settings import settings


@dataclass
class ConnectionInfo:
    connection: IMAP4_SSL
    account: Account
    last_used: float
    is_idle: bool = False
    selected_folder: str | None = None


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


# TODO: Make dynamic.
IMAP_HOSTS = ["imap.purelymail.com"]


class ConnectionManager:
    """Manages IMAP connections with pooling and rate limiting."""

    def __init__(self) -> None:
        self._logger = logging.getLogger(__name__)
        self._connections: dict[str, list[ConnectionInfo]] = {}  # provider -> connections
        self._rate_limiters: dict[str, RateLimiter] = {}
        self._connection_locks: dict[str, asyncio.Semaphore] = {}
        self._lock = asyncio.Lock()

        limit = 10
        for imap_hosts in IMAP_HOSTS:
            self._connection_locks[imap_hosts] = asyncio.Semaphore(limit)
            self._rate_limiters[imap_hosts] = RateLimiter(rate=limit - 1, burst=limit)

    async def get_connection(self, account: Account, folder: str | None = None) -> IMAP4_SSL | None:
        """Get an IMAP connection for the account, reusing if possible."""
        imap_provider = account.provider_context.get("imap_host")
        if not imap_provider:
            raise ValueError("IMAP provider not found in account context")

        # Rate limiting
        await self._rate_limiters[imap_provider].acquire()

        # Try to reuse existing connection
        connection_info = await self._find_reusable_connection(account, folder)
        if connection_info:
            return connection_info.connection

        # Create new connection if under limit
        async with self._connection_locks[imap_provider]:
            return await self._create_new_connection(account, folder)

    async def _find_reusable_connection(self, account: Account, folder: str | None = None) -> ConnectionInfo | None:
        """Find a reusable connection for the account."""
        imap_host = account.provider_context.get("imap_host")

        if imap_host not in self._connections:
            return None

        async with self._lock:
            for conn_info in self._connections[imap_host]:
                if (
                    conn_info.account.id == account.id
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
                        self._connections[imap_host].remove(conn_info)

        return None

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

            # Store connection info
            conn_info = ConnectionInfo(
                connection=connection, account=account, last_used=time.time(), selected_folder=folder
            )

            async with self._lock:
                if imap_host not in self._connections:
                    self._connections[imap_host] = []
                self._connections[imap_host].append(conn_info)

            self._logger.info(f"Created new IMAP connection for {account.email}:{folder}")
            return connection

        except Exception as e:
            self._logger.error(f"Failed to create IMAP connection for {account.email}: {e}")
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

    async def start_idle(self, connection: IMAP4_SSL, account: Account) -> None:
        """Mark connection as in IDLE mode."""
        imap_host = account.provider_context.get("imap_host")
        if not imap_host:
            raise ValueError("IMAP host not found in account context")

        async with self._lock:
            for conn_info in self._connections.get(imap_host, []):
                if conn_info.connection == connection:
                    conn_info.is_idle = True
                    break

    async def stop_idle(self, connection: IMAP4_SSL, account: Account) -> None:
        """Mark connection as no longer in IDLE mode."""
        imap_host = account.provider_context.get("imap_host")
        if not imap_host:
            raise ValueError("IMAP host not found in account context")

        async with self._lock:
            for conn_info in self._connections.get(imap_host, []):
                if conn_info.connection == connection:
                    conn_info.is_idle = False
                    break

    async def release_connection(self, connection: IMAP4_SSL, account: Account) -> None:
        """Release a connection back to the pool."""
        imap_host = account.provider_context.get("imap_host")
        if not imap_host:
            raise ValueError("IMAP host not found in account context")

        async with self._lock:
            for conn_info in self._connections.get(imap_host, []):
                if conn_info.connection == connection:
                    conn_info.is_idle = False
                    conn_info.last_used = time.time()
                    break

    async def close_connection(self, connection: IMAP4_SSL, account: Account) -> None:
        """Close and remove a connection from the pool."""
        imap_host = account.provider_context.get("imap_host")
        if not imap_host:
            raise ValueError("IMAP host not found in account context")

        # Remove connection from pool
        connection_found = False
        async with self._lock:
            for conn_info in self._connections.get(imap_host, []):
                if conn_info.connection == connection:
                    self._connections[imap_host].remove(conn_info)
                    connection_found = True
                    break

        # Close the connection (even if not found in pool, in case it's already been removed)
        try:
            await asyncio.wait_for(connection.logout(), timeout=5)
            if connection_found:
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

    async def cleanup_idle_connections(self, max_idle_time: int = 600) -> None:
        """Clean up connections that have been idle too long."""
        current_time = time.time()
        connections_to_close: list[tuple[IMAP4_SSL, Account]] = []

        async with self._lock:
            for provider, conn_list in self._connections.items():
                for conn_info in conn_list[:]:  # Copy list to avoid modification during iteration
                    if current_time - conn_info.last_used > max_idle_time:
                        connections_to_close.append((conn_info.connection, conn_info.account))
                        conn_list.remove(conn_info)

        # Close idle connections
        for connection, account in connections_to_close:
            try:
                await self.close_connection(connection, account)
                self._logger.info(f"Closed idle connection for {account.email}")
            except Exception as e:
                self._logger.warning(f"Error closing idle connection for {account.email}: {e}")

    async def get_connection_stats(self) -> dict[str, dict[str, int]]:
        """Get statistics about current connections."""
        stats = {}

        async with self._lock:
            for provider, conn_list in self._connections.items():
                stats[provider] = {
                    "total": len(conn_list),
                    "idle": sum(1 for conn in conn_list if conn.is_idle),
                    "active": sum(1 for conn in conn_list if not conn.is_idle),
                }

        return stats

    async def close_all_connections(self) -> None:
        """Close all connections in the pool."""
        connections_to_close: list[tuple[IMAP4_SSL, Account]] = []

        async with self._lock:
            for _, conn_list in self._connections.items():
                for conn_info in conn_list:
                    connections_to_close.append((conn_info.connection, conn_info.account))
                conn_list.clear()

        # Close all connections with timeout per connection
        for connection, account in connections_to_close:
            try:
                await asyncio.wait_for(self.close_connection(connection, account), timeout=10)
            except asyncio.TimeoutError:
                self._logger.warning(f"Timeout closing connection for {account.email}, skipping")
            except Exception as e:
                self._logger.warning(f"Error closing connection: {e}")

        self._logger.info("All IMAP connections closed")
