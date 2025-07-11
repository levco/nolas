"""
Middleware for automatic database commits at the end of each request.
"""

import logging
from typing import Awaitable, Callable

from fastapi import Request, Response
from fastapi_async_sqlalchemy import db
from fastapi_async_sqlalchemy.exceptions import MissingSessionError
from starlette.middleware.base import BaseHTTPMiddleware

logger = logging.getLogger(__name__)


class AutoCommitMiddleware(BaseHTTPMiddleware):
    """
    Middleware that automatically commits database transactions at the end of each request.

    This middleware ensures that all database changes are committed automatically,
    eliminating the need for manual commit calls in repositories and controllers.
    """

    async def dispatch(self, request: Request, call_next: Callable[[Request], Awaitable[Response]]) -> Response:
        """
        Process the request and automatically commit the database transaction.

        Args:
            request: The incoming request
            call_next: The next middleware/handler in the chain

        Returns:
            The response from the request handler
        """
        try:
            # Process the request
            response = await call_next(request)

            try:
                session = db.session
                if session is not None:
                    await session.commit()
                    logger.debug("Database transaction committed successfully")
            except MissingSessionError:
                # No session exists for this request, which is fine for endpoints that don't use the database
                logger.debug("No database session found for request - skipping commit")
            except Exception as e:
                logger.warning(f"Failed to commit database transaction: {e}")

            return response

        except Exception as e:
            # If there's an error, try to rollback the transaction
            try:
                session = db.session
                if session is not None:
                    await session.rollback()
                    logger.error(f"Database transaction rolled back due to error: {e}")
            except MissingSessionError:
                logger.debug("No database session found for rollback - skipping rollback")
            except Exception as e:
                logger.warning(f"Failed to rollback database transaction: {e}")

            raise
