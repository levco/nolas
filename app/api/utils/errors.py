import uuid
from typing import Any

from fastapi.responses import JSONResponse

from app.api.models.error import APIError, ErrorDetail


def create_error_response(
    error_type: str, message: str, status_code: int, provider_error: dict[str, Any] | None = None
) -> JSONResponse:
    """
    Create a structured error response that matches the Nylas API schema.

    Args:
        error_type: The type of error (e.g., "invalid_request_error", "api.internal_error")
        message: Human-readable error message
        status_code: HTTP status code
        provider_error: Optional provider-specific error details

    Returns:
        JSONResponse with structured error format
    """
    error_response = APIError(
        request_id=str(uuid.uuid4()), error=ErrorDetail(type=error_type, message=message, provider_error=provider_error)
    )
    return JSONResponse(status_code=status_code, content=error_response.model_dump())
