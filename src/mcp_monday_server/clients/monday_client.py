"""Monday.com GraphQL API client."""

import os
from functools import lru_cache
from typing import Any, Dict, Optional

from ..config import load_config
from ..exceptions import MondayAPIError
from ..logging_config import get_logger, log_with_context
from .base_client import BaseHTTPClient

logger = get_logger(__name__)

MONDAY_API_URL = "https://api.monday.com"
MONDAY_API_PATH = "/v2"
MONDAY_API_VERSION = "2025-01"


class MondayClient(BaseHTTPClient):
    """
    Async Monday.com GraphQL API client.

    Wraps BaseHTTPClient to provide a single `graphql()` method
    that sends queries/mutations to the Monday.com v2 API endpoint.
    All requests are authenticated via Bearer token.
    """

    def __init__(
        self,
        api_key: str,
        timeout: int = 30,
        max_retries: int = 3,
        workspace_url: Optional[str] = None,
    ):
        """
        Initialize Monday.com client.

        Args:
            api_key: Monday.com Personal API token
            timeout: Request timeout in seconds
            max_retries: Maximum retry attempts
            workspace_url: Optional workspace base URL for building item links
        """
        super().__init__(
            base_url=MONDAY_API_URL,
            timeout=timeout,
            max_retries=max_retries,
            headers={
                "Authorization": api_key,
                "Content-Type": "application/json",
                "API-Version": MONDAY_API_VERSION,
            },
        )
        self.workspace_url = workspace_url

        log_with_context(
            logger,
            "info",
            "Monday.com client initialized",
            api_version=MONDAY_API_VERSION,
            timeout=timeout,
            max_retries=max_retries,
        )

    async def graphql(self, query: str) -> Dict[str, Any]:
        """
        Execute a GraphQL query or mutation against the Monday.com API.

        Args:
            query: GraphQL query or mutation string

        Returns:
            Parsed response data dict (the value of the top-level "data" key)

        Raises:
            MondayAPIError: On HTTP errors or GraphQL-level errors
        """
        response = await self.post(MONDAY_API_PATH, json={"query": query})
        payload: Dict[str, Any] = response.json()

        # GraphQL errors are returned with HTTP 200 but include an "errors" key
        if "errors" in payload and payload["errors"]:
            messages = "; ".join(e.get("message", str(e)) for e in payload["errors"])
            log_with_context(logger, "error", "GraphQL errors in response", errors=payload["errors"])
            raise MondayAPIError(f"Monday.com GraphQL error: {messages}")

        data = payload.get("data")
        if data is None:
            raise MondayAPIError("Monday.com API returned no data", response_body=str(payload))

        return data

    def build_item_url(self, board_id: str, item_id: str) -> str:
        """
        Build a direct URL to a Monday.com item.

        Args:
            board_id: Board ID
            item_id: Item ID

        Returns:
            Full URL to the item, or a placeholder if workspace_url is not set
        """
        if self.workspace_url:
            return f"{self.workspace_url}/boards/{board_id}/pulses/{item_id}"
        return f"(workspace URL not configured) Board {board_id} / Item {item_id}"

    def build_doc_url(self, doc_id: str) -> str:
        """
        Build a direct URL to a Monday.com document.

        Args:
            doc_id: Document ID

        Returns:
            Full URL to the document, or a placeholder if workspace_url is not set
        """
        if self.workspace_url:
            return f"{self.workspace_url}/docs/{doc_id}"
        return f"(workspace URL not configured) Doc ID {doc_id}"


# Module-level singleton — created lazily on first call
_monday_client: Optional[MondayClient] = None


def get_monday_client() -> MondayClient:
    """
    Return the shared MondayClient singleton.

    Reads configuration from environment / config.yaml on first call.
    Subsequent calls return the cached instance.

    Returns:
        MondayClient instance

    Raises:
        ValueError: If MCP_MONDAY_API_KEY is not configured
    """
    global _monday_client
    if _monday_client is None:
        config = load_config(os.getenv("MCP_MONDAY_CONFIG_PATH", "config.yaml"))
        _monday_client = MondayClient(
            api_key=config.monday.get_api_key(),
            timeout=config.monday.timeout,
            max_retries=config.monday.max_retries,
            workspace_url=config.monday.workspace_url,
        )
    return _monday_client


