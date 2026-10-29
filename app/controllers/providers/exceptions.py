def is_rate_limit_response(status_code: int, body: str) -> bool:
    return status_code == 429 or (
        status_code == 403
        and any(
            marker in body.lower()
            for marker in ("ratelimitexceeded", "rate_limit_exceeded", "rate limit", "quota", "resource_exhausted")
        )
    )


class ProviderError(Exception):
    """Base error for provider API failures."""

    def __init__(self, message: str, status_code: int = 502, provider_code: str | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.status_code = status_code
        self.provider_code = provider_code


class ProviderAuthError(ProviderError):
    """The grant's credentials are no longer valid (revoked/expired refresh token)."""

    def __init__(self, message: str = "Provider credentials are invalid or expired.") -> None:
        super().__init__(message, status_code=401, provider_code="invalid_grant")


class ProviderNotFoundError(ProviderError):
    """The requested object does not exist at the provider."""

    def __init__(self, message: str = "Requested object not found.") -> None:
        super().__init__(message, status_code=404, provider_code="NotFoundError")


class ProviderRateLimitError(ProviderError):
    """The provider rejected the request due to rate limiting."""

    def __init__(
        self, message: str = "Provider rate limit exceeded.", *, retry_after_seconds: int | None = None
    ) -> None:
        super().__init__(message, status_code=429, provider_code="RateLimitError")
        self.retry_after_seconds = retry_after_seconds
