"""
FastAPI application entry point - Nylas-compatible API
"""

from contextlib import asynccontextmanager
from typing import AsyncGenerator, Dict

from fastapi import FastAPI
from fastapi_async_sqlalchemy import SQLAlchemyMiddleware

from app.api.routes import api_router
from app.container import get_wire_container
from settings.settings import Settings


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    """Application lifespan manager."""
    # Startup
    print("Starting up Nolas API...")

    # Initialize dependency injection container
    container = get_wire_container()
    app.state.container = container

    yield

    # Shutdown
    print("Shutting down Nolas API...")


def create_app() -> FastAPI:
    """Create and configure FastAPI application."""
    settings = Settings()

    app = FastAPI(
        title="Nolas API",
        description="Nylas-compatible email API",
        version="1.0.0",
        lifespan=lifespan,
    )

    # Add SQLAlchemy middleware for database session management
    database_url = f"{settings.database.async_host}/{settings.database.name}"
    app.add_middleware(
        SQLAlchemyMiddleware,
        db_url=database_url,
        engine_args={
            "pool_size": settings.database.min_pool_size,
            "max_overflow": settings.database.max_pool_size - settings.database.min_pool_size,
            "pool_pre_ping": True,
            "pool_recycle": 300,
        },
    )

    # Include API routers
    app.include_router(api_router, prefix="/v3")

    # Health check endpoint
    @app.get("/health")
    async def health_check() -> Dict[str, str]:
        """Health check endpoint."""
        return {"status": "healthy", "service": "nolas-api"}

    return app


# Create the app instance
app = create_app()
