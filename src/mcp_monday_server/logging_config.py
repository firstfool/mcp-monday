"""Structured logging configuration with security features."""

import json
import logging
import re
import sys
from contextvars import ContextVar
from datetime import datetime
from typing import Any, Dict, Set

# Context variable for correlation ID
correlation_id_var: ContextVar[str] = ContextVar("correlation_id", default="")

# Patterns to redact from logs
SENSITIVE_PATTERNS = [
    (re.compile(r'(api[_-]?key["\s:=]+)([^\s,}"]+)', re.IGNORECASE), r"\1***REDACTED***"),
    (re.compile(r'(password["\s:=]+)([^\s,}"]+)', re.IGNORECASE), r"\1***REDACTED***"),
    (re.compile(r'(token["\s:=]+)([^\s,}"]+)', re.IGNORECASE), r"\1***REDACTED***"),
    (re.compile(r'(secret["\s:=]+)([^\s,}"]+)', re.IGNORECASE), r"\1***REDACTED***"),
    (re.compile(r'(authorization["\s:=]+bearer\s+)([^\s,}"]+)', re.IGNORECASE), r"\1***REDACTED***"),
    (re.compile(r"(basic\s+)([A-Za-z0-9+/=]+)", re.IGNORECASE), r"\1***REDACTED***"),
]

# Fields to redact completely
SENSITIVE_FIELDS: Set[str] = {
    "api_key",
    "apikey",
    "password",
    "token",
    "secret",
    "authorization",
    "auth",
    "credentials",
    "passwd",
    "pwd",
}


def redact_sensitive_data(data: Any) -> Any:
    """
    Recursively redact sensitive data from logs.

    This function prevents accidental logging of credentials, API keys,
    tokens, and other sensitive information.

    Args:
        data: Data to redact (dict, list, str, etc.)

    Returns:
        Redacted data with sensitive information replaced
    """
    if isinstance(data, dict):
        return {
            key: "***REDACTED***" if key.lower() in SENSITIVE_FIELDS else redact_sensitive_data(value)
            for key, value in data.items()
        }
    elif isinstance(data, list):
        return [redact_sensitive_data(item) for item in data]
    elif isinstance(data, str):
        for pattern, replacement in SENSITIVE_PATTERNS:
            data = pattern.sub(replacement, data)
        return data
    else:
        return data


class JSONFormatter(logging.Formatter):
    """JSON log formatter for structured logging with automatic sensitive data redaction."""

    def format(self, record: logging.LogRecord) -> str:
        """Format log record as JSON with security redaction."""
        log_data: Dict[str, Any] = {
            "timestamp": datetime.utcnow().isoformat() + "Z",
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            "module": record.module,
            "function": record.funcName,
            "line": record.lineno,
        }

        correlation_id = correlation_id_var.get()
        if correlation_id:
            log_data["correlation_id"] = correlation_id

        if record.exc_info:
            log_data["exception"] = self.formatException(record.exc_info)

        if hasattr(record, "extra_fields"):
            log_data.update(redact_sensitive_data(record.extra_fields))  # type: ignore[attr-defined]

        return json.dumps(redact_sensitive_data(log_data))


class TextFormatter(logging.Formatter):
    """Human-readable text formatter for development with security redaction."""

    def format(self, record: logging.LogRecord) -> str:
        """Format log record as text with security redaction."""
        correlation_id = correlation_id_var.get()
        correlation_str = f"[{correlation_id}] " if correlation_id else ""
        message = redact_sensitive_data(record.getMessage())
        timestamp = datetime.utcnow().isoformat() + "Z"
        return f"{timestamp} - {record.name} - {record.levelname} - {correlation_str}{message}"


def setup_logging(level: str = "INFO", format_type: str = "json") -> None:
    """
    Configure structured logging for the application.

    Args:
        level: Log level (DEBUG, INFO, WARNING, ERROR, CRITICAL)
        format_type: Log format type (json or text)
    """
    root_logger = logging.getLogger()
    for handler in root_logger.handlers[:]:
        root_logger.removeHandler(handler)

    handler = logging.StreamHandler(sys.stdout)

    if format_type.lower() == "json":
        formatter: logging.Formatter = JSONFormatter()
    else:
        formatter = TextFormatter()

    handler.setFormatter(formatter)
    root_logger.addHandler(handler)
    root_logger.setLevel(getattr(logging, level.upper()))

    # Quieten noisy libraries
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)


def get_logger(name: str) -> logging.Logger:
    """
    Get a logger instance.

    Args:
        name: Logger name (typically __name__)

    Returns:
        Logger instance
    """
    return logging.getLogger(name)


def set_correlation_id(correlation_id: str) -> None:
    """Set correlation ID for current context."""
    correlation_id_var.set(correlation_id)


def get_correlation_id() -> str:
    """Get correlation ID from current context."""
    return correlation_id_var.get()


def log_with_context(logger: logging.Logger, level: str, message: str, **kwargs: Any) -> None:
    """
    Log message with additional context fields.

    Args:
        logger: Logger instance
        level: Log level
        message: Log message
        **kwargs: Additional context fields
    """
    extra = {"extra_fields": kwargs}
    getattr(logger, level.lower())(message, extra=extra)

