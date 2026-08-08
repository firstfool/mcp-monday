"""get_all_boards_data — Fetch the complete Monday.com workspace dataset.

This is the single MCP tool exposed by this server. It returns all boards,
groups, columns, and items in one structured JSON response, using the local
SQLite sync database for change detection to avoid redundant API calls.

Typical usage by the ICA assistant:
  1. Call get_all_boards_data() at the start of a conversation turn.
  2. Receive the full dataset (from cache if nothing changed, fresh sync otherwise).
  3. Reason over the data locally to answer the user's query.
"""

import re
import uuid
from typing import Any, Dict, List, Optional

from chuk_mcp_runtime.common.mcp_tool_decorator import mcp_tool

from ..exceptions import MondayMCPError
from ..logging_config import get_logger, log_with_context, set_correlation_id
from ..sync.engine import get_sync_engine

logger = get_logger(__name__)

# Numeric board IDs only — prevents any GraphQL injection
_BOARD_ID_RE = re.compile(r"^\d+$")


def _validate_board_ids(board_ids: Optional[List[str]]) -> Optional[List[str]]:
    """
    Validate and sanitise optional board ID filter.

    Returns the cleaned list, or raises ValueError on invalid input.
    """
    if board_ids is None:
        return None
    if not isinstance(board_ids, list):
        raise ValueError("board_ids must be a list of numeric strings")
    cleaned = []
    for bid in board_ids:
        bid = str(bid).strip()
        if not _BOARD_ID_RE.match(bid):
            raise ValueError(
                f"Invalid board_id '{bid}': board IDs must be numeric strings (e.g. '123456')"
            )
        cleaned.append(bid)
    return cleaned or None  # treat empty list same as None


@mcp_tool()
async def get_all_boards_data(
    board_ids: Optional[List[str]] = None,
    force_refresh: bool = False,
) -> Dict[str, Any]:
    """
    Fetch all Monday.com board data — boards, groups, columns, and items.

    Data is served from a local SQLite cache. The cache is refreshed only when
    Monday.com reports a newer ``updated_at`` timestamp than the last sync,
    or when the cache is older than the configured ``max_age_hours``.

    Use this tool at the start of every conversation turn to give the assistant
    full workspace context before answering user queries about boards or cards.

    IMPORTANT — RETRY BEHAVIOUR:
    On the very first call after a cold start, the server must fetch all data
    from Monday.com (a full sync). This can take 60–120 seconds for large
    workspaces. If the tool returns ``error_code: "SYNC_IN_PROGRESS"`` or the
    call times out, the sync is still running in the background. You MUST call
    this tool again after waiting ``retry_after_seconds`` (default 10 s). Keep
    retrying until ``success`` is true. Once the first sync completes, every
    subsequent call returns instantly from the local cache
    (``sync_status: "cache_hit"``).

    Args:
        board_ids: Optional list of numeric board IDs to scope the response
                   (e.g. ["123456", "789012"]). Omit to fetch all boards.
        force_refresh: If True, bypass change detection and always re-sync
                       from the Monday.com API.

    Returns:
        On success:
        {
          "success": true,
          "sync_status": "cache_hit" | "synced" | "sync_failed_stale_data_returned",
          "synced_at": "2025-07-18T10:00:00Z",
          "latest_updated_at": "2025-07-17T15:30:00Z" | null,
          "board_count": int,
          "item_count": int,
          "boards": [
            {
              "id": "123456",
              "name": "Phoenix",
              "groups": [{"id": "g1", "title": "In Progress"}, ...],
              "columns": [{"id": "status", "title": "Status", "type": "color"}, ...],
              "items": [
                {
                  "id": "789",
                  "name": "Fix login bug",
                  "group_id": "g1",
                  "group_title": "In Progress",
                  "column_values": {"Status": "Working on it", "Due Date": "2025-07-25"},
                  "updated_at": "2025-07-17T15:30:00Z"
                },
                ...
              ]
            },
            ...
          ]
        }

        On sync in progress (RETRY REQUIRED — call again after retry_after_seconds):
        {
          "success": false,
          "error_code": "SYNC_IN_PROGRESS",
          "error_message": "Initial sync is running. Please retry in a few seconds.",
          "retry_after_seconds": 10
        }

        On failure (no cached data available and API unreachable):
        {
          "success": false,
          "error_code": "API_ERROR" | "INTERNAL_ERROR",
          "error_message": "..."
        }
    """
    correlation_id = str(uuid.uuid4())
    set_correlation_id(correlation_id)
    log_with_context(
        logger, "info", "Processing get_all_boards_data",
        correlation_id=correlation_id,
        board_ids=board_ids,
        force_refresh=force_refresh,
    )

    # Validate board_ids before touching the API or DB
    try:
        validated_board_ids = _validate_board_ids(board_ids)
    except ValueError as exc:
        log_with_context(logger, "warning", "Invalid board_ids", error=str(exc))
        return {
            "success": False,
            "error_code": "VALIDATION_ERROR",
            "error_message": str(exc),
        }

    try:
        engine = get_sync_engine()
        result = await engine.run(board_ids=validated_board_ids, force=force_refresh)

        if not result.success:
            # Special case: initial sync is running — no data available yet
            if result.sync_in_progress:
                log_with_context(
                    logger, "info", "get_all_boards_data: initial sync in progress",
                    correlation_id=correlation_id,
                )
                return {
                    "success": False,
                    "error_code": "SYNC_IN_PROGRESS",
                    "error_message": result.error_message,
                    "retry_after_seconds": 10,
                }

            log_with_context(
                logger, "error", "get_all_boards_data failed",
                correlation_id=correlation_id,
                error_code=result.error_code,
                error=result.error_message,
            )
            return {
                "success": False,
                "error_code": result.error_code or "UNKNOWN_ERROR",
                "error_message": result.error_message or "Unknown error",
            }

        # Map SyncResult.cache_hit / synced / stale_fallback → sync_status string
        if result.stale_fallback:
            sync_status = "sync_failed_stale_data_returned"
        elif result.cache_hit:
            sync_status = "cache_hit"
        else:
            sync_status = "synced"

        log_with_context(
            logger, "info", "get_all_boards_data completed",
            correlation_id=correlation_id,
            sync_status=sync_status,
            board_count=result.board_count,
            item_count=result.item_count,
        )

        response: Dict[str, Any] = {
            "success": True,
            "sync_status": sync_status,
            "synced_at": result.synced_at,
            "latest_updated_at": result.latest_updated_at,
            "board_count": result.board_count,
            "item_count": result.item_count,
            "boards": result.boards_data,
        }

        # Include a warning message when stale data is returned
        if result.stale_fallback and result.error_message:
            response["warning"] = result.error_message

        return response

    except RuntimeError as exc:
        # SyncEngine not initialised — server startup issue
        log_with_context(logger, "error", "SyncEngine not available", error=str(exc))
        return {
            "success": False,
            "error_code": "SERVER_ERROR",
            "error_message": "Server not properly initialised. Check server logs.",
        }
    except Exception as exc:
        log_with_context(
            logger, "error", "Unexpected error in get_all_boards_data",
            correlation_id=correlation_id, error=str(exc),
        )
        return {
            "success": False,
            "error_code": "INTERNAL_ERROR",
            "error_message": f"Internal error: {exc}",
        }
