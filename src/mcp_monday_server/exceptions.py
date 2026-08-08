"""Custom exceptions for Monday.com MCP Server."""

from typing import Any, Dict, Optional


class MondayMCPError(Exception):
    """
    Base exception for Monday.com MCP Server.

    All custom exceptions inherit from this class.
    """

    def __init__(
        self,
        message: str,
        error_code: str = "UNKNOWN_ERROR",
        details: Optional[Dict[str, Any]] = None,
    ):
        """
        Initialize exception.

        Args:
            message: Human-readable error message
            error_code: Machine-readable error code
            details: Additional error details
        """
        self.message = message
        self.error_code = error_code
        self.details = details or {}
        super().__init__(message)


class AuthenticationError(MondayMCPError):
    """Authentication failure — API key missing or invalid."""

    def __init__(
        self,
        message: str = "Authentication failed",
        details: Optional[Dict[str, Any]] = None,
    ):
        """
        Initialize authentication error.

        Args:
            message: Error message
            details: Additional details
        """
        super().__init__(message=message, error_code="AUTH_ERROR", details=details)


class TimeoutError(MondayMCPError):
    """Request timeout error."""

    def __init__(
        self,
        message: str = "Request timed out",
        timeout_seconds: Optional[int] = None,
    ):
        """
        Initialize timeout error.

        Args:
            message: Error message
            timeout_seconds: Timeout value that was exceeded
        """
        details: Dict[str, Any] = {}
        if timeout_seconds:
            details["timeout_seconds"] = timeout_seconds

        super().__init__(message=message, error_code="TIMEOUT_ERROR", details=details)


class MondayAPIError(MondayMCPError):
    """Monday.com GraphQL API communication error."""

    def __init__(
        self,
        message: str,
        status_code: Optional[int] = None,
        response_body: Optional[str] = None,
        is_retryable: bool = False,
    ):
        """
        Initialize API error.

        Args:
            message: Error message
            status_code: HTTP status code
            response_body: Response body (will be truncated)
            is_retryable: Whether error is retryable
        """
        self.status_code = status_code
        self.response_body = response_body
        self.is_retryable = is_retryable

        details: Dict[str, Any] = {"is_retryable": is_retryable}
        if status_code:
            details["status_code"] = status_code
        if response_body:
            details["response_preview"] = response_body[:200]

        super().__init__(message=message, error_code="API_ERROR", details=details)

