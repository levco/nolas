import enum
from http import HTTPStatus
from typing import Any


class ErrorType(enum.Enum):
    ENTITY_ALREADY_EXISTS = "entity_already_exists"
    ENTITY_NOT_FOUND = "entity_not_found"
    FORBIDDEN = "forbidden"
    INTERNAL_ERROR = "internal_error"
    INVALID_DATA = "invalid_data"
    INVALID_REQUEST_HEADER = "invalid_request_header"
    INVALID_STATE = "invalid_state"
    NOT_SUPORTTED = "not_supported"
    THIRD_PARTY_REQUEST = "third_party_request"
    UNHANDLED_EXCEPTION = "unhandled_exception"
    UNAUTHORIZED_USER = "unauthorized_user"
    UNAUHTORIZED_REQUEST = "unauthorized_request"
    UNSPECIFIED = "unspecified"


class BaseError(Exception):
    extra: dict[str, Any]

    def __init__(
        self,
        message: str,
        error_type: ErrorType = ErrorType.UNSPECIFIED,
        status_code: HTTPStatus = HTTPStatus.INTERNAL_SERVER_ERROR,
        **kwargs: Any,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.error_type = error_type
        self.status_code = status_code
        self.extra = {}

        action = kwargs.get("action")
        if action:
            self.extra["action"] = action
        user = kwargs.get("user")
        if user:
            self.extra["user"] = user

    def __str__(self) -> str:
        return f"error: {self.error_type.value}; description: {self.message}"


class AuthError(BaseError):
    def __init__(
        self,
        message: str,
        error_type: ErrorType = ErrorType.UNAUTHORIZED_USER,
        status_code: HTTPStatus = HTTPStatus.UNAUTHORIZED,
        **kwargs: Any,
    ) -> None:
        super().__init__(message, error_type, status_code, **kwargs)


class ActionForbiddenError(BaseError):
    def __init__(
        self,
        message: str,
        error_type: ErrorType = ErrorType.FORBIDDEN,
        status_code: HTTPStatus = HTTPStatus.FORBIDDEN,
        **kwargs: Any,
    ) -> None:
        super().__init__(message, error_type, status_code, **kwargs)


class EntityAlreadyExistError(BaseError):
    def __init__(
        self,
        message: str,
        error_type: ErrorType = ErrorType.ENTITY_ALREADY_EXISTS,
        status_code: HTTPStatus = HTTPStatus.BAD_REQUEST,
        **kwargs: Any,
    ) -> None:
        super().__init__(message, error_type, status_code, **kwargs)


class EntityNotFoundError(BaseError):
    def __init__(
        self,
        message: str,
        error_type: ErrorType = ErrorType.ENTITY_NOT_FOUND,
        status_code: HTTPStatus = HTTPStatus.NOT_FOUND,
        **kwargs: Any,
    ) -> None:
        super().__init__(message, error_type, status_code, **kwargs)


class InvalidDataError(BaseError):
    def __init__(
        self,
        message: str,
        error_type: ErrorType = ErrorType.INVALID_DATA,
        status_code: HTTPStatus = HTTPStatus.BAD_REQUEST,
        **kwargs: Any,
    ) -> None:
        super().__init__(message, error_type, status_code, **kwargs)


class InternalError(BaseError):
    def __init__(
        self,
        message: str,
        error_type: ErrorType = ErrorType.INTERNAL_ERROR,
        status_code: HTTPStatus = HTTPStatus.INTERNAL_SERVER_ERROR,
        **kwargs: Any,
    ) -> None:
        super().__init__(message, error_type, status_code, **kwargs)


class BusinessLogicError(BaseError):
    def __init__(
        self,
        message: str,
        error_type: ErrorType = ErrorType.INTERNAL_ERROR,
        status_code: HTTPStatus = HTTPStatus.INTERNAL_SERVER_ERROR,
        **kwargs: Any,
    ) -> None:
        super().__init__(message, error_type, status_code, **kwargs)


class NotSupportedError(BaseError):
    def __init__(
        self,
        message: str,
        error_type: ErrorType = ErrorType.NOT_SUPORTTED,
        status_code: HTTPStatus = HTTPStatus.INTERNAL_SERVER_ERROR,
        **kwargs: Any,
    ) -> None:
        super().__init__(message, error_type, status_code, **kwargs)


class TransactionError(BaseError):
    def __init__(
        self,
        message: str,
        error_type: ErrorType,
        status_code: HTTPStatus = HTTPStatus.INTERNAL_SERVER_ERROR,
        **kwargs: Any,
    ) -> None:
        super().__init__(message, error_type, status_code, **kwargs)


class ActionError(BaseError):
    def __init__(
        self,
        message: str,
        error_type: ErrorType = ErrorType.THIRD_PARTY_REQUEST,
        status_code: HTTPStatus = HTTPStatus.INTERNAL_SERVER_ERROR,
        **kwargs: Any,
    ) -> None:
        super().__init__(message, error_type, status_code, **kwargs)


class WhatsAppError(BaseError):
    def __init__(
        self,
        message: str,
        error_type: ErrorType = ErrorType.THIRD_PARTY_REQUEST,
        status_code: HTTPStatus = HTTPStatus.INTERNAL_SERVER_ERROR,
        **kwargs: Any,
    ) -> None:
        super().__init__(message, error_type, status_code, **kwargs)


class BancardError(BaseError):
    def __init__(
        self,
        message: str,
        error_type: ErrorType = ErrorType.THIRD_PARTY_REQUEST,
        status_code: HTTPStatus = HTTPStatus.INTERNAL_SERVER_ERROR,
        **kwargs: Any,
    ) -> None:
        super().__init__(message, error_type, status_code, **kwargs)


class UenoError(BaseError):
    def __init__(
        self,
        message: str,
        error_type: ErrorType = ErrorType.THIRD_PARTY_REQUEST,
        status_code: HTTPStatus = HTTPStatus.INTERNAL_SERVER_ERROR,
        **kwargs: Any,
    ) -> None:
        super().__init__(message, error_type, status_code, **kwargs)


class UenoAnauthorizedError(BaseError):
    def __init__(
        self,
        message: str,
        error_type: ErrorType = ErrorType.UNAUHTORIZED_REQUEST,
        status_code: HTTPStatus = HTTPStatus.UNAUTHORIZED,
        **kwargs: Any,
    ) -> None:
        super().__init__(message, error_type, status_code, **kwargs)
