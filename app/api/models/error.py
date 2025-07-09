from typing import Any

from pydantic import BaseModel


class ErrorDetail(BaseModel):
    """Error detail model matching Nylas API error schema."""

    type: str
    message: str
    provider_error: dict[str, Any] | None = None


class APIError(BaseModel):
    """Error response model that matches Nylas API schema."""

    request_id: str
    error: ErrorDetail
