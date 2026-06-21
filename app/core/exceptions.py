from __future__ import annotations


class AppException(Exception):
    """애플리케이션 베이스 예외."""

    code: str = "INTERNAL_ERROR"
    status_code: int = 500

    def __init__(self, message: str, *, code: str | None = None) -> None:
        super().__init__(message)
        self.message = message
        if code:
            self.code = code


class NotFoundError(AppException):
    code = "NOT_FOUND"
    status_code = 404


class ValidationError(AppException):
    code = "VALIDATION_ERROR"
    status_code = 400


class TokenLimitExceededError(AppException):
    code = "TOKEN_LIMIT_EXCEEDED"
    status_code = 400


class RateLimitExceededError(AppException):
    code = "RATE_LIMIT_EXCEEDED"
    status_code = 429

    def __init__(self, message: str, *, retry_after_ms: int = 0) -> None:
        super().__init__(message)
        self.retry_after_ms = retry_after_ms


class LLMClientError(AppException):
    code = "LLM_CLIENT_ERROR"
    status_code = 502


class ExternalServiceError(AppException):
    code = "EXTERNAL_SERVICE_ERROR"
    status_code = 502
