"""Domain errors mapped onto OpenAI-shaped HTTP error payloads."""

from __future__ import annotations

from typing import Any


class ServiceError(Exception):
    """Base class for errors that carry an HTTP mapping."""

    http_status: int = 500
    error_type: str = "server_error"
    code: str = "internal_error"
    retry_after: float | None = None

    def __init__(self, message: str, *, retry_after: float | None = None) -> None:
        super().__init__(message)
        self.message = message
        if retry_after is not None:
            self.retry_after = retry_after

    def to_payload(self) -> dict[str, Any]:
        """Render as an OpenAI-shaped error body, so clients can parse it as usual."""
        return {
            "error": {
                "message": self.message,
                "type": self.error_type,
                "code": self.code,
                "param": None,
            }
        }


class InvalidRequestError(ServiceError):
    http_status = 400
    error_type = "invalid_request_error"
    code = "invalid_request"


class AuthenticationError(ServiceError):
    http_status = 401
    error_type = "invalid_request_error"
    code = "invalid_api_key"


class ModelNotFoundError(ServiceError):
    http_status = 404
    error_type = "invalid_request_error"
    code = "model_not_found"


class AdmissionError(ServiceError):
    """Request refused before it ever reached the engine."""

    http_status = 503
    error_type = "server_overloaded"
    code = "overloaded"
    reason = "admission_error"


class QueueFullError(AdmissionError):
    http_status = 429
    error_type = "rate_limit_error"
    code = "queue_full"
    reason = "queue_full"
    retry_after = 1.0


class QueueTimeoutError(AdmissionError):
    http_status = 503
    code = "queue_timeout"
    reason = "queue_timeout"
    retry_after = 2.0


class ShuttingDownError(AdmissionError):
    http_status = 503
    code = "shutting_down"
    reason = "shutting_down"
    retry_after = 5.0


class RequestTimeoutError(ServiceError):
    http_status = 504
    error_type = "server_error"
    code = "request_timeout"


class EngineError(ServiceError):
    http_status = 500
    error_type = "server_error"
    code = "engine_error"
