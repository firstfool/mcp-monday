"""Base HTTP client with retry logic, connection pooling, and TLS security."""

import asyncio
import os
import random
from datetime import datetime
from typing import Any, Dict, Optional, Tuple

import certifi
import httpx

from ..exceptions import MondayAPIError, TimeoutError
from ..logging_config import get_logger, log_with_context

logger = get_logger(__name__)


class BaseHTTPClient:
    """
    Base HTTP client with enterprise features and security.

    Features:
    - Connection pooling
    - Automatic retries with exponential backoff and jitter
    - Timeout management
    - TLS/SSL configuration with certifi CA bundle
    - Basic authentication support
    - Request/response logging
    - Error handling
    """

    def __init__(
        self,
        base_url: str,
        timeout: int = 30,
        max_retries: int = 3,
        verify_ssl: bool = True,
        ca_bundle: Optional[str] = None,
        headers: Optional[Dict[str, str]] = None,
        auth: Optional[Tuple[str, str]] = None,
    ):
        """
        Initialize HTTP client with TLS security.

        Args:
            base_url: Base URL for API requests
            timeout: Request timeout in seconds
            max_retries: Maximum number of retry attempts
            verify_ssl: Whether to verify SSL certificates
            ca_bundle: Path to custom CA bundle (optional)
            headers: Default headers for all requests
            auth: Basic authentication tuple (username, password)
        """
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.max_retries = max_retries
        self.default_headers = headers or {}
        self.auth = auth

        if verify_ssl:
            if ca_bundle:
                verify: bool | str = ca_bundle
                logger.info(f"Using custom CA bundle: {ca_bundle}")
            else:
                verify = certifi.where()
                logger.debug(f"Using certifi CA bundle: {verify}")
        else:
            verify = False
            environment = os.getenv("MCP_MONDAY_ENVIRONMENT", "production")
            if environment == "production":
                logger.warning(
                    "SSL verification is disabled in production environment. "
                    "This is insecure and not recommended."
                )

        limits = httpx.Limits(
            max_keepalive_connections=10,
            max_connections=20,
            keepalive_expiry=30.0,
        )

        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(timeout),
            limits=limits,
            verify=verify,
            auth=auth,
            headers=self.default_headers,
            follow_redirects=True,
        )

        logger.info(
            "HTTP client initialized",
            extra={
                "extra_fields": {
                    "base_url": base_url,
                    "timeout": timeout,
                    "max_retries": max_retries,
                    "verify_ssl": verify_ssl,
                }
            },
        )

    async def close(self) -> None:
        """Close the HTTP client and cleanup connections."""
        await self._client.aclose()
        logger.debug("HTTP client closed")

    async def __aenter__(self) -> "BaseHTTPClient":
        """Async context manager entry."""
        return self

    async def __aexit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        """Async context manager exit."""
        await self.close()

    def _calculate_backoff(self, attempt: int) -> float:
        """
        Calculate exponential backoff delay with jitter.

        Args:
            attempt: Current attempt number (0-based)

        Returns:
            Delay in seconds
        """
        base_delay = 1.0
        max_delay = 30.0
        delay = min(base_delay * (2**attempt), max_delay)
        jitter = random.uniform(0, 0.1 * delay)
        return delay + jitter

    def _is_retryable_error(self, error: Exception) -> bool:
        """
        Determine if an error is retryable.

        Args:
            error: Exception that occurred

        Returns:
            True if error is retryable
        """
        if isinstance(error, (httpx.NetworkError, httpx.TimeoutException)):
            return True
        if isinstance(error, httpx.HTTPStatusError):
            retryable_status_codes = {408, 429, 500, 502, 503, 504}
            return error.response.status_code in retryable_status_codes
        return False

    async def request(
        self,
        method: str,
        path: str,
        headers: Optional[Dict[str, str]] = None,
        params: Optional[Dict[str, Any]] = None,
        json: Optional[Dict[str, Any]] = None,
        data: Optional[Dict[str, Any]] = None,
    ) -> httpx.Response:
        """
        Make HTTP request with retry logic.

        Args:
            method: HTTP method (GET, POST, etc.)
            path: API path (will be appended to base_url)
            headers: Additional headers for this request
            params: Query parameters
            json: JSON body
            data: Form data

        Returns:
            HTTP response

        Raises:
            TimeoutError: If request times out after all retries
            MondayAPIError: If request fails after all retries
        """
        url = f"{self.base_url}/{path.lstrip('/')}"
        request_headers = {**self.default_headers, **(headers or {})}
        last_error: Optional[Exception] = None

        for attempt in range(self.max_retries + 1):
            try:
                start_time = datetime.utcnow()

                log_with_context(
                    logger,
                    "debug",
                    f"HTTP {method} request",
                    url=url,
                    attempt=attempt + 1,
                    max_attempts=self.max_retries + 1,
                )

                response = await self._client.request(
                    method=method,
                    url=url,
                    headers=request_headers,
                    params=params,
                    json=json,
                    data=data,
                )

                duration = (datetime.utcnow() - start_time).total_seconds()
                log_with_context(
                    logger,
                    "debug",
                    f"HTTP {method} response",
                    url=url,
                    status_code=response.status_code,
                    duration_seconds=duration,
                )

                response.raise_for_status()
                return response

            except httpx.TimeoutException as e:
                last_error = e
                log_with_context(logger, "warning", "Request timeout", url=url, attempt=attempt + 1, timeout=self.timeout)

                if attempt < self.max_retries:
                    delay = self._calculate_backoff(attempt)
                    log_with_context(logger, "info", "Retrying after timeout", delay_seconds=delay)
                    await asyncio.sleep(delay)
                    continue

                raise TimeoutError(f"Request timed out after {self.max_retries + 1} attempts")

            except httpx.HTTPStatusError as e:
                last_error = e
                log_with_context(
                    logger,
                    "warning",
                    "HTTP error response",
                    url=url,
                    status_code=e.response.status_code,
                    attempt=attempt + 1,
                )

                if not self._is_retryable_error(e):
                    raise MondayAPIError(
                        f"HTTP {e.response.status_code}: {e.response.text}",
                        status_code=e.response.status_code,
                        response_body=e.response.text,
                    )

                if attempt < self.max_retries:
                    delay = self._calculate_backoff(attempt)
                    log_with_context(logger, "info", "Retrying after HTTP error", delay_seconds=delay)
                    await asyncio.sleep(delay)
                    continue

                raise MondayAPIError(
                    f"HTTP {e.response.status_code} after {self.max_retries + 1} attempts",
                    status_code=e.response.status_code,
                    response_body=e.response.text,
                )

            except httpx.NetworkError as e:
                last_error = e
                log_with_context(logger, "warning", "Network error", url=url, attempt=attempt + 1, error=str(e))

                if attempt < self.max_retries:
                    delay = self._calculate_backoff(attempt)
                    log_with_context(logger, "info", "Retrying after network error", delay_seconds=delay)
                    await asyncio.sleep(delay)
                    continue

                raise MondayAPIError(f"Network error after {self.max_retries + 1} attempts: {str(e)}")

        raise MondayAPIError(
            f"Request failed after {self.max_retries + 1} attempts",
            status_code=None,
            response_body=str(last_error) if last_error else None,
        )

    async def get(self, path: str, **kwargs: Any) -> httpx.Response:
        """Make GET request."""
        return await self.request("GET", path, **kwargs)

    async def post(self, path: str, **kwargs: Any) -> httpx.Response:
        """Make POST request."""
        return await self.request("POST", path, **kwargs)

    async def put(self, path: str, **kwargs: Any) -> httpx.Response:
        """Make PUT request."""
        return await self.request("PUT", path, **kwargs)

    async def patch(self, path: str, **kwargs: Any) -> httpx.Response:
        """Make PATCH request."""
        return await self.request("PATCH", path, **kwargs)

    async def delete(self, path: str, **kwargs: Any) -> httpx.Response:
        """Make DELETE request."""
        return await self.request("DELETE", path, **kwargs)

