from contextlib import asynccontextmanager
from typing import AsyncGenerator

from fastapi_async_sqlalchemy import SQLAlchemyMiddleware, db
from starlette.applications import Starlette

from settings import settings


@asynccontextmanager
async def fastapi_sqlalchemy_context() -> AsyncGenerator[None, None]:
    """Initialize fastapi_async_sqlalchemy for standalone scripts."""

    database_url = f"{settings.database.async_host}/{settings.database.name}"

    # Create a minimal Starlette app to initialize the middleware
    app = Starlette()
    SQLAlchemyMiddleware(
        app,
        db_url=database_url,
        engine_args={
            "echo": False,
            "future": True,
            "pool_size": settings.database.min_pool_size,
            "max_overflow": settings.database.max_pool_size - settings.database.min_pool_size,
        },
    )

    async with db():
        yield
