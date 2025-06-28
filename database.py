import asyncio
import logging

import asyncpg
from asyncpg import Pool

from models import AccountConfig
from settings import settings

logger = logging.getLogger(__name__)


class DatabaseManager:
    """Async PostgreSQL database manager with connection pooling."""

    def __init__(self) -> None:
        self.pool: Pool | None = None
        self._lock = asyncio.Lock()

    async def init_pool(self, database_url: str | None = None) -> None:
        """Initialize the connection pool."""
        if self.pool is not None:
            return

        async with self._lock:
            if self.pool is not None:
                return

            db_url = database_url or f"{settings.database.host}/{settings.database.name}"
            min_pool_size = settings.database.min_pool_size
            max_pool_size = settings.database.max_pool_size

            try:
                self.pool = await asyncpg.create_pool(
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

                # Initialize schema
                await self._init_schema()

            except Exception as e:
                logger.error(f"Failed to initialize database pool: {e}")
                raise

    async def close_pool(self) -> None:
        """Close the connection pool."""
        if self.pool:
            await self.pool.close()
            self.pool = None
            logger.info("Database pool closed")

    async def _init_schema(self) -> None:
        """Initialize database schema."""
        schema_sql = """
        -- Accounts table
        CREATE TABLE IF NOT EXISTS accounts (
            id SERIAL PRIMARY KEY,
            email VARCHAR(255) UNIQUE NOT NULL,
            username VARCHAR(255) NOT NULL,
            password_encrypted TEXT NOT NULL,
            provider VARCHAR(100) NOT NULL,
            webhook_url TEXT NOT NULL,
            is_active BOOLEAN DEFAULT true,
            created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
            updated_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
        );
        
        -- UID tracking table
        CREATE TABLE IF NOT EXISTS uid_tracking (
            account_email VARCHAR(255) NOT NULL,
            folder VARCHAR(255) NOT NULL,
            last_seen_uid BIGINT NOT NULL DEFAULT 0,
            last_checked_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
            created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
            PRIMARY KEY (account_email, folder)
        );
        
        -- Connection health tracking
        CREATE TABLE IF NOT EXISTS connection_health (
            account_email VARCHAR(255) NOT NULL,
            folder VARCHAR(255) NOT NULL,
            last_success_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
            consecutive_failures INTEGER DEFAULT 0,
            last_error TEXT,
            is_active BOOLEAN DEFAULT true,
            PRIMARY KEY (account_email, folder)
        );
        
        -- Webhook delivery tracking
        CREATE TABLE IF NOT EXISTS webhook_logs (
            id SERIAL PRIMARY KEY,
            account_email VARCHAR(255) NOT NULL,
            folder VARCHAR(255) NOT NULL,
            uid BIGINT NOT NULL,
            webhook_url TEXT NOT NULL,
            status_code INTEGER,
            response_body TEXT,
            attempts INTEGER DEFAULT 1,
            delivered_at TIMESTAMP WITH TIME ZONE,
            created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
        );
        
        -- Indexes for performance
        CREATE INDEX IF NOT EXISTS idx_uid_tracking_account_folder ON uid_tracking(account_email, folder);
        CREATE INDEX IF NOT EXISTS idx_connection_health_active ON connection_health(is_active, last_success_at);
        CREATE INDEX IF NOT EXISTS idx_webhook_logs_account_created ON webhook_logs(account_email, created_at DESC);
        
        -- Update trigger for updated_at
        CREATE OR REPLACE FUNCTION update_updated_at_column()
        RETURNS TRIGGER AS $$
        BEGIN
            NEW.updated_at = NOW();
            RETURN NEW;
        END;
        $$ language 'plpgsql';
        
        DROP TRIGGER IF EXISTS update_accounts_updated_at ON accounts;
        CREATE TRIGGER update_accounts_updated_at
            BEFORE UPDATE ON accounts
            FOR EACH ROW
            EXECUTE FUNCTION update_updated_at_column();
        """
        if not self.pool:
            raise RuntimeError("Database pool not initialized")
        async with self.pool.acquire() as conn:
            await conn.execute(schema_sql)
            logger.info("Database schema initialized")

    async def get_last_seen_uid(self, account_email: str, folder: str) -> int:
        """Get the last seen UID for an account/folder combination."""
        if not self.pool:
            raise RuntimeError("Database pool not initialized")
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT last_seen_uid FROM uid_tracking WHERE account_email = $1 AND folder = $2", account_email, folder
            )
            return row["last_seen_uid"] if row else 0

    async def update_last_seen_uid(self, account_email: str, folder: str, uid: int) -> None:
        """Update the last seen UID for an account/folder combination."""
        if not self.pool:
            raise RuntimeError("Database pool not initialized")
        async with self.pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO uid_tracking (account_email, folder, last_seen_uid, last_checked_at)
                VALUES ($1, $2, $3, NOW())
                ON CONFLICT (account_email, folder) 
                DO UPDATE SET 
                    last_seen_uid = GREATEST(uid_tracking.last_seen_uid, $3),
                    last_checked_at = NOW()
            """,
                account_email,
                folder,
                uid,
            )

    async def get_active_accounts(self) -> list[AccountConfig]:
        """Retrieve all active accounts from the database."""
        if not self.pool:
            raise RuntimeError("Database pool not initialized")
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT email, username, password_encrypted, provider, webhook_url FROM accounts WHERE is_active = true"
            )

            accounts = []
            for row in rows:
                # In production, you'd decrypt the password here
                accounts.append(
                    AccountConfig(
                        email=row["email"],
                        username=row["username"],
                        password=row["password_encrypted"],  # TODO: Implement decryption
                        provider=row["provider"],
                        webhook_url=row["webhook_url"],
                    )
                )

            return accounts

    async def add_account(self, account: AccountConfig) -> None:
        """Add a new account to the database."""
        if not self.pool:
            raise RuntimeError("Database pool not initialized")
        async with self.pool.acquire() as conn:
            # In production, encrypt the password before storing
            await conn.execute(
                """
                INSERT INTO accounts (email, username, password_encrypted, provider, webhook_url)
                VALUES ($1, $2, $3, $4, $5)
                ON CONFLICT (email) DO UPDATE SET
                    username = $2,
                    password_encrypted = $3,
                    provider = $4,
                    webhook_url = $5,
                    updated_at = NOW()
            """,
                account.email,
                account.username,
                account.password,
                account.provider,
                account.webhook_url,
            )

    async def record_connection_health(
        self, account_email: str, folder: str, success: bool, error_message: str | None = None
    ) -> None:
        """Record connection health status."""
        if not self.pool:
            raise RuntimeError("Database pool not initialized")
        async with self.pool.acquire() as conn:
            if success:
                await conn.execute(
                    """
                    INSERT INTO connection_health (account_email, folder, last_success_at, consecutive_failures, is_active)
                    VALUES ($1, $2, NOW(), 0, true)
                    ON CONFLICT (account_email, folder) DO UPDATE SET
                        last_success_at = NOW(),
                        consecutive_failures = 0,
                        last_error = NULL,
                        is_active = true
                """,
                    account_email,
                    folder,
                )
            else:
                await conn.execute(
                    """
                    INSERT INTO connection_health (account_email, folder, consecutive_failures, last_error, is_active)
                    VALUES ($1, $2, 1, $3, true)
                    ON CONFLICT (account_email, folder) DO UPDATE SET
                        consecutive_failures = connection_health.consecutive_failures + 1,
                        last_error = $3,
                        is_active = CASE WHEN connection_health.consecutive_failures + 1 < 5 THEN true ELSE false END
                """,
                    account_email,
                    folder,
                    error_message,
                )

    async def log_webhook_delivery(
        self,
        account_email: str,
        folder: str,
        uid: int,
        webhook_url: str,
        status_code: int | None = None,
        response_body: str | None = None,
        attempts: int = 1,
        delivered: bool = False,
    ) -> None:
        """Log webhook delivery attempt."""
        if not self.pool:
            raise RuntimeError("Database pool not initialized")
        async with self.pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO webhook_logs (account_email, folder, uid, webhook_url, status_code, 
                                        response_body, attempts, delivered_at)
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
            """,
                account_email,
                folder,
                uid,
                webhook_url,
                status_code,
                response_body,
                attempts,
                asyncio.get_event_loop().time() if delivered else None,
            )

    async def get_failed_connections(self, max_failures: int = 3) -> list[dict[str, str | int]]:
        """Get connections that have failed multiple times."""
        if not self.pool:
            raise RuntimeError("Database pool not initialized")
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT account_email, folder, consecutive_failures, last_error, last_success_at
                FROM connection_health 
                WHERE consecutive_failures >= $1 AND is_active = false
                ORDER BY consecutive_failures DESC, last_success_at ASC
            """,
                max_failures,
            )

            return [dict(row) for row in rows]

    async def cleanup_old_logs(self, days: int = 30) -> int:
        """Clean up old webhook logs."""
        if not self.pool:
            raise RuntimeError("Database pool not initialized")
        async with self.pool.acquire() as conn:
            result = await conn.execute(
                """
                DELETE FROM webhook_logs 
                WHERE created_at < NOW() - INTERVAL '%s days'
            """,
                days,
            )

            deleted_count = int(result.split()[-1])
            logger.info(f"Cleaned up {deleted_count} old webhook logs")
            return deleted_count
