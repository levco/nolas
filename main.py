"""
FastAPI application entry point - Nylas-compatible API
"""

import logging
from contextlib import asynccontextmanager
from typing import Any, AsyncGenerator

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from fastapi_async_sqlalchemy import SQLAlchemyMiddleware

from app.api.routes import api_router
from app.container import get_wire_container
from app.exceptions import BaseError, ErrorType
from environment import EnvironmentName
from settings import settings

logger = logging.getLogger(__name__)


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


def _setup_error_handlers(app: FastAPI) -> None:
    """Setup FastAPI exception handlers."""

    @app.exception_handler(HTTPException)
    async def handle_http_error(request: Request, exc: HTTPException) -> JSONResponse:
        """Handle FastAPI HTTPException errors."""
        return JSONResponse(status_code=exc.status_code or 400, content={"error": exc.detail})

    @app.exception_handler(BaseError)
    async def handle_app_error(request: Request, exc: BaseError) -> JSONResponse:
        """Handle custom BaseError exceptions."""
        if exc.status_code >= 400 and exc.status_code < 500:
            logger.warning(f"A user-related (HTTP 4xx) error occurred; {exc}", exc_info=True, extra=exc.extra)
        else:
            logger.exception(f"An unhandled app exception occurred; {exc}", extra=exc.extra)

        return JSONResponse(
            status_code=exc.status_code, content={"error": exc.error_type.value, "error_description": exc.message}
        )

    @app.exception_handler(Exception)
    async def handle_generic_error(request: Request, exc: Exception) -> JSONResponse:
        """Handle any unhandled exceptions."""
        if settings.environment == EnvironmentName.TESTING:
            logging.exception(f"An unhandled exception occurred; error: {exc}")
        logger.exception(f"An unhandled exception occurred; error: {exc}")

        return JSONResponse(status_code=500, content={"error": ErrorType.UNHANDLED_EXCEPTION.value})


def create_app() -> FastAPI:
    """Create and configure FastAPI application."""
    app = FastAPI(
        title="Nolas API",
        description="Nylas-compatible email API",
        version="1.0.0",
        lifespan=lifespan,
    )

    # Configure OpenAPI security scheme for Bearer token
    def custom_openapi() -> dict[str, Any]:
        if app.openapi_schema:
            return app.openapi_schema

        from fastapi.openapi.utils import get_openapi

        openapi_schema = get_openapi(
            title="Nolas API",
            version="1.0.0",
            description="Nylas-compatible email API",
            routes=app.routes,
        )

        # Add Bearer token security scheme
        openapi_schema["components"]["securitySchemes"] = {
            "BearerAuth": {
                "type": "http",
                "scheme": "bearer",
                "bearerFormat": "JWT",
                "description": "Enter your Bearer token (without 'Bearer ' prefix)",
            }
        }

        # Apply security to all API endpoints (but not health check)
        for path in openapi_schema["paths"]:
            for method in openapi_schema["paths"][path]:
                if method in ["get", "post", "put", "delete", "patch"]:
                    # Skip health check endpoint
                    if path == "/health":
                        continue
                    openapi_schema["paths"][path][method]["security"] = [{"BearerAuth": []}]

        app.openapi_schema = openapi_schema
        return app.openapi_schema

    # Override the openapi method
    setattr(app, "openapi", custom_openapi)

    # Setup error handlers
    _setup_error_handlers(app)

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
    async def health_check() -> dict[str, str]:
        """Health check endpoint."""
        return {"status": "healthy", "service": "nolas-api"}

    return app


# Create the app instance
app = create_app()
