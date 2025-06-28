import logging
from typing import AsyncGenerator

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool

from app.models import Base
from settings import settings

logger = logging.getLogger(__name__)


class DatabaseManager:
    """SQLAlchemy async database manager."""

    _engine: AsyncEngine | None
    _session_factory: async_sessionmaker[AsyncSession] | None

    def __init__(self) -> None:
        self._engine = None
        self._session_factory = None

    def init_db(self, database_url: str | None = None) -> None:
        """Initialize the database engine and session factory."""
        if self._engine is not None:
            return

        # Build database URL
        if database_url:
            db_url = database_url
        else:
            # Convert postgresql:// to postgresql+asyncpg://
            base_url = settings.database.host.replace("postgresql://", "postgresql+asyncpg://")
            db_url = f"{base_url}/{settings.database.name}"

        # Create async engine
        self._engine = create_async_engine(
            db_url,
            echo=settings.database.echo if hasattr(settings.database, "echo") else False,
            poolclass=NullPool if getattr(settings.database, "use_null_pool", False) else None,
            pool_size=settings.database.min_pool_size if hasattr(settings.database, "min_pool_size") else 5,
            max_overflow=settings.database.max_pool_size - settings.database.min_pool_size
            if hasattr(settings.database, "max_pool_size")
            else 10,
        )

        self._session_factory = async_sessionmaker(
            bind=self._engine,
            class_=AsyncSession,
            expire_on_commit=False,
        )

        logger.info("Database engine and session factory initialized")

    async def create_tables(self) -> None:
        """Create all tables."""
        if not self._engine:
            raise RuntimeError("Database not initialized. Call init_db() first.")

        async with self._engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

        logger.info("Database tables created")

    async def drop_tables(self) -> None:
        """Drop all tables."""
        if not self._engine:
            raise RuntimeError("Database not initialized. Call init_db() first.")

        async with self._engine.begin() as conn:
            await conn.run_sync(Base.metadata.drop_all)

        logger.info("Database tables dropped")

    async def close(self) -> None:
        """Close the database engine."""
        if self._engine:
            await self._engine.dispose()
            self._session_factory = None
            logger.info("Database engine closed")

    async def get_session(self) -> AsyncGenerator[AsyncSession, None]:
        """Get an async database session."""
        if not self._session_factory:
            raise RuntimeError("Database not initialized. Call init_db() first.")

        async with self._session_factory() as session:
            try:
                yield session
            except Exception:
                await session.rollback()
                raise
            finally:
                await session.close()


# Global database manager instance
db_manager = DatabaseManager()


async def get_db_session() -> AsyncGenerator[AsyncSession, None]:
    """Dependency for getting database sessions."""
    async for session in db_manager.get_session():
        yield session
