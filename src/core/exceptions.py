"""Application-wide exception hierarchy.

Services raise these; the global handler in src.main maps them to JSON
responses with the appropriate status code. Routers should NOT catch these —
let them propagate to the handler.
"""

from __future__ import annotations


class AppError(Exception):
    """Base for all application-level errors with an HTTP status code."""

    status_code: int = 400
    code: str = "app_error"

    def __init__(self, message: str, *, code: str | None = None, status_code: int | None = None):
        super().__init__(message)
        self.message = message
        if code is not None:
            self.code = code
        if status_code is not None:
            self.status_code = status_code


class ValidationError(AppError):
    status_code = 422
    code = "validation_error"


class NotFoundError(AppError):
    status_code = 404
    code = "not_found"


class ForbiddenError(AppError):
    status_code = 403
    code = "forbidden"


class UnauthorizedError(AppError):
    status_code = 401
    code = "unauthorized"


class ConflictError(AppError):
    status_code = 409
    code = "conflict"


class RateLimitError(AppError):
    status_code = 429
    code = "rate_limited"


class SubscriptionError(ForbiddenError):
    """Raised when an action requires a paid subscription the user doesn't hold."""

    code = "subscription_required"


__all__ = [
    "AppError",
    "ValidationError",
    "NotFoundError",
    "ForbiddenError",
    "UnauthorizedError",
    "ConflictError",
    "RateLimitError",
    "SubscriptionError",
]
