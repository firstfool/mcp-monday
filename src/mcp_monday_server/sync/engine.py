"""Sync engine — change detection and full board data fetch for Monday.com.

Flow on every get_all_boards_data call:
  1. Try to acquire asyncio.Lock with timeout (serialises concurrent requests)
     - If lock times out and DB has data → return stale data immediately
     - If lock times out and DB is empty → return sync_in_progress status
  2. Read last sync info from SyncDB
  3. Check DB age (safety valve: force re-sync if older than max_age_seconds)
  4. Fetch latest updated_at from Monday (1 lightweight API call)
  5. If unchanged → return full dataset from SQLite (0 more API calls)
  6. If changed → full fetch → atomic write → return fresh dataset
  7. Release lock

Concurrency within a single process:
  All invocations share one asyncio.Lock with a configurable wait timeout.
  - The first caller performs the sync.
  - Subsequent callers wait up to max_sync_wait_seconds.
  - If they wait successfully, they read the freshly written DB (cache hit).
  - If the timeout fires (e.g. very slow first sync), they return stale data
    or a sync_in_progress status — never hang indefinitely.

Serverless / multi-instance:
  Each container instance has its own in-memory or /tmp SQLite DB and its own
  asyncio.Lock. Two instances that start simultaneously will each perform one
  full sync independently. This is safe (read-only against Monday.com) and
  bounded — Monday.com API is not bombarded because each instance only syncs
  once, not on every call.
  Recommendation: configure db_path=':memory:' for serverless (no filesystem
  persistence needed; the DB is rebuilt on first call per instance lifetime).
"""

import asyncio
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from ..clients.monday_client import MondayClient
from ..exceptions import MondayMCPError
from ..logging_config import get_logger, log_with_context
from .db import SyncDB

logger = get_logger(__name__)

# Monday.com API allows a maximum of 500 items per page
_MAX_PAGE_SIZE = 500

# Default maximum seconds to wait for the sync lock before returning
# stale data or a sync_in_progress status.
_DEFAULT_LOCK_TIMEOUT = 120  # 2 minutes


@dataclass
class SyncResult:
    """Result of a SyncEngine.run() call."""

    success: bool
    cache_hit: bool = False
    synced: bool = False
    # True when sync is already running in another coroutine and caller timed out
    sync_in_progress: bool = False
    synced_at: Optional[str] = None
    latest_updated_at: Optional[str] = None
    boards_data: List[Dict[str, Any]] = field(default_factory=list)
    board_count: int = 0
    item_count: int = 0
    error_code: Optional[str] = None
    error_message: Optional[str] = None
    # True when sync failed but stale DB data was returned as a fallback
    stale_fallback: bool = False


class SyncEngine:
    """
    Orchestrates change detection and full data sync against Monday.com.

    All public methods are async. The internal asyncio.Lock guarantees that
    only one sync is ever running at a time within the same process.
    """

    def __init__(
        self,
        client: MondayClient,
        db: SyncDB,
        max_age_seconds: int = 86400,  # 24 h
        items_page_size: int = 500,
        max_sync_wait_seconds: int = _DEFAULT_LOCK_TIMEOUT,
    ) -> None:
        self._client = client
        self._db = db
        self._max_age_seconds = max_age_seconds
        self._items_page_size = min(items_page_size, _MAX_PAGE_SIZE)
        self._max_sync_wait_seconds = max_sync_wait_seconds
        self._lock = asyncio.Lock()

    # ── Change detection ───────────────────────────────────────────────────

    async def get_latest_monday_updated_at(
        self, board_ids: Optional[List[str]] = None
    ) -> Optional[str]:
        """
        Return the ISO 8601 UTC timestamp of the most recently updated item
        across all (or specified) boards.

        This is exactly **one** GraphQL API call.

        Returns:
            ISO string or None if no items exist yet.
        """
        if board_ids:
            ids_str = ", ".join(board_ids)
            query = (
                f"{{ boards(ids: [{ids_str}], limit: {len(board_ids)}) "
                "{ items_page(limit: 1) { items { id updated_at } } } }"
            )
        else:
            query = (
                "{ boards(limit: 500, page: 1) "
                "{ items_page(limit: 1) { items { id updated_at } } } }"
            )

        data = await self._client.graphql(query)
        boards = data.get("boards", [])

        latest: Optional[str] = None
        for board in boards:
            items = board.get("items_page", {}).get("items", [])
            for item in items:
                ts = item.get("updated_at")
                if ts and (latest is None or ts > latest):
                    latest = ts

        return latest

    # ── Full fetch helpers ─────────────────────────────────────────────────

    async def _fetch_board_list(
        self, board_ids: Optional[List[str]] = None
    ) -> List[Dict[str, str]]:
        """Return [{id, name}] for all (or specified) boards."""
        if board_ids:
            ids_str = ", ".join(board_ids)
            query = f"{{ boards(ids: [{ids_str}], limit: {len(board_ids)}) {{ id name }} }}"
            data = await self._client.graphql(query)
            return data.get("boards", [])

        # Paginate through all boards (max 500 per page)
        all_boards = []
        page = 1
        while True:
            data = await self._client.graphql(
                f"{{ boards(limit: 500, page: {page}) {{ id name }} }}"
            )
            batch = data.get("boards", [])
            if not batch:
                break
            all_boards.extend(batch)
            page += 1
        return all_boards

    async def _fetch_board_schema(
        self, board_id: str
    ) -> Dict[str, Any]:
        """Fetch groups and columns for one board."""
        data = await self._client.graphql(
            f"""{{
                boards(ids: {board_id}) {{
                    groups {{ id title }}
                    columns {{ id title type }}
                }}
            }}"""
        )
        boards = data.get("boards", [])
        if not boards:
            return {"groups": [], "columns": []}
        return {
            "groups": boards[0].get("groups", []),
            "columns": [
                {"id": c["id"], "title": c["title"], "type": c["type"]}
                for c in boards[0].get("columns", [])
            ],
        }

    async def _fetch_board_items(
        self,
        board_id: str,
        groups: List[Dict[str, str]],
        columns: List[Dict[str, str]],
    ) -> List[Dict[str, Any]]:
        """
        Fetch all items for one board using cursor pagination.

        column_values are flattened to {column_title: text_value}.
        Empty / null text values are excluded to reduce payload size.

        Uses only ``id`` and ``text`` on ColumnValue — ``title`` was removed
        from the Monday.com API in the 2024-10 version. Column titles are
        resolved from the board's columns schema instead.
        """
        group_lookup = {g["id"]: g["title"] for g in groups}
        # Map column id → human-readable title from the schema we already have
        col_title_lookup = {c["id"]: c["title"] for c in columns}
        all_items: List[Dict[str, Any]] = []
        cursor: Optional[str] = None

        while True:
            cursor_part = f'cursor: "{cursor}"' if cursor else ""
            data = await self._client.graphql(
                f"""{{
                    boards(ids: {board_id}) {{
                        items_page({cursor_part} limit: {self._items_page_size}) {{
                            cursor
                            items {{
                                id name updated_at
                                group {{ id }}
                                column_values {{ id text }}
                            }}
                        }}
                    }}
                }}"""
            )

            boards = data.get("boards", [])
            page = boards[0].get("items_page", {}) if boards else {}
            items = page.get("items", [])
            cursor = page.get("cursor")  # None when last page reached

            for item in items:
                group_id = (item.get("group") or {}).get("id", "")
                group_title = group_lookup.get(group_id, "")

                # Flatten column_values: {column_title: text}, skip blank values.
                # Use col_title_lookup to resolve id → title; fall back to id.
                col_vals: Dict[str, str] = {}
                for cv in item.get("column_values", []):
                    text = cv.get("text") or ""
                    if text.strip():
                        col_id = cv.get("id") or ""
                        title = col_title_lookup.get(col_id) or col_id
                        if title:
                            col_vals[title] = text

                all_items.append({
                    "id": item["id"],
                    "name": item.get("name", ""),
                    "group_id": group_id,
                    "group_title": group_title,
                    "column_values": col_vals,
                    "updated_at": item.get("updated_at"),
                })

            if not cursor:
                break

        return all_items

    async def fetch_all_boards(
        self, board_ids: Optional[List[str]] = None
    ) -> List[Dict[str, Any]]:
        """
        Fetch the complete dataset: boards → groups + columns → items (paginated).

        Boards are fetched sequentially (not concurrently) to avoid
        hammering the Monday.com API rate limits.

        Returns:
            List of board dicts ready for ``SyncDB.write_full_sync()``.
        """
        board_list = await self._fetch_board_list(board_ids)
        log_with_context(
            logger, "info", "Starting full board fetch",
            board_count=len(board_list),
        )

        results: List[Dict[str, Any]] = []
        for i, board in enumerate(board_list, start=1):
            bid = board["id"]
            log_with_context(
                logger, "debug", "Fetching board schema + items",
                board_id=bid, board_name=board["name"],
                progress=f"{i}/{len(board_list)}",
            )
            schema = await self._fetch_board_schema(bid)
            items = await self._fetch_board_items(bid, schema["groups"], schema["columns"])

            results.append({
                "id": bid,
                "name": board["name"],
                "groups": schema["groups"],
                "columns": schema["columns"],
                "items": items,
            })

        total_items = sum(len(b["items"]) for b in results)
        log_with_context(
            logger, "info", "Full board fetch complete",
            board_count=len(results),
            item_count=total_items,
        )
        return results

    # ── Main entry point ───────────────────────────────────────────────────

    async def run(
        self,
        board_ids: Optional[List[str]] = None,
        force: bool = False,
    ) -> SyncResult:
        """
        Run change detection and sync if needed.

        Returns a SyncResult with the full dataset (from DB or fresh fetch).

        Args:
            board_ids: Optional list of board IDs to scope the operation.
            force: If True, skip change detection and always re-sync.
        """
        try:
            await asyncio.wait_for(
                self._lock.acquire(), timeout=self._max_sync_wait_seconds
            )
        except asyncio.TimeoutError:
            # Another coroutine is holding the lock — return stale data or signal
            # that an initial sync is in progress.
            last_sync = self._db.get_last_sync_info()
            if last_sync is not None:
                dataset = self._db.get_full_dataset(board_ids)
                return SyncResult(
                    success=True,
                    stale_fallback=True,
                    synced_at=last_sync["synced_at"],
                    latest_updated_at=last_sync["latest_updated_at"],
                    boards_data=dataset,
                    board_count=len(dataset),
                    item_count=self._db.get_item_count(board_ids),
                    error_message="Sync in progress; returning cached data",
                )
            return SyncResult(
                success=False,
                sync_in_progress=True,
                error_code="SYNC_IN_PROGRESS",
                error_message="Initial sync is running. Please retry in a few seconds.",
            )
        try:
            return await self._run_locked(board_ids=board_ids, force=force)
        finally:
            self._lock.release()

    async def _run_locked(
        self,
        board_ids: Optional[List[str]],
        force: bool,
    ) -> SyncResult:
        """Called under self._lock — never call directly."""
        now_str = datetime.now(tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

        # ── Step 1: decide whether a sync is needed ────────────────────────
        last_sync = self._db.get_last_sync_info()
        db_age = self._db.get_db_age_seconds()
        needs_sync = force

        if not needs_sync:
            if last_sync is None:
                log_with_context(logger, "info", "No previous sync found — triggering full sync")
                needs_sync = True
            elif db_age is not None and db_age > self._max_age_seconds:
                log_with_context(
                    logger, "info", "DB age exceeds max_age — triggering full sync",
                    db_age_seconds=int(db_age),
                    max_age_seconds=self._max_age_seconds,
                )
                needs_sync = True

        # ── Step 2: change detection (1 API call) ──────────────────────────
        monday_latest: Optional[str] = None
        if not needs_sync:
            try:
                monday_latest = await self.get_latest_monday_updated_at(board_ids)
                stored_latest = last_sync["latest_updated_at"] if last_sync else None  # type: ignore[index]

                if monday_latest == stored_latest:
                    log_with_context(
                        logger, "info", "Cache hit — data unchanged",
                        latest_updated_at=monday_latest,
                    )
                    dataset = self._db.get_full_dataset(board_ids)
                    return SyncResult(
                        success=True,
                        cache_hit=True,
                        synced_at=last_sync["synced_at"],  # type: ignore[index]
                        latest_updated_at=monday_latest,
                        boards_data=dataset,
                        board_count=len(dataset),
                        item_count=self._db.get_item_count(board_ids),
                    )
                else:
                    log_with_context(
                        logger, "info", "Data changed — triggering full sync",
                        stored_latest=stored_latest,
                        monday_latest=monday_latest,
                    )
                    needs_sync = True
            except MondayMCPError as exc:
                # Change detection API call failed; fall back to stale DB if available
                log_with_context(
                    logger, "warning",
                    "Change detection failed — serving stale data if available",
                    error=str(exc),
                )
                if last_sync is not None:
                    dataset = self._db.get_full_dataset(board_ids)
                    return SyncResult(
                        success=True,
                        stale_fallback=True,
                        synced_at=last_sync["synced_at"],
                        latest_updated_at=last_sync["latest_updated_at"],
                        boards_data=dataset,
                        board_count=len(dataset),
                        item_count=self._db.get_item_count(board_ids),
                        error_message=f"Change detection failed: {exc}; returning cached data",
                    )
                return SyncResult(
                    success=False,
                    error_code=exc.error_code,
                    error_message=str(exc),
                )

        # ── Step 3: full fetch + write ─────────────────────────────────────
        try:
            boards_data = await self.fetch_all_boards(board_ids)

            # Compute the newest updated_at across all fetched items
            latest_ts: Optional[str] = monday_latest  # may already be set from step 2
            if latest_ts is None:
                for board in boards_data:
                    for item in board.get("items", []):
                        ts = item.get("updated_at")
                        if ts and (latest_ts is None or ts > latest_ts):
                            latest_ts = ts

            await self._db.write_full_sync(
                boards_data=boards_data,
                synced_at=now_str,
                latest_updated_at=latest_ts,
            )

            item_count = sum(len(b["items"]) for b in boards_data)
            log_with_context(
                logger, "info", "Sync complete",
                synced_at=now_str,
                latest_updated_at=latest_ts,
                board_count=len(boards_data),
                item_count=item_count,
            )
            return SyncResult(
                success=True,
                synced=True,
                synced_at=now_str,
                latest_updated_at=latest_ts,
                boards_data=boards_data,
                board_count=len(boards_data),
                item_count=item_count,
            )

        except MondayMCPError as exc:
            log_with_context(logger, "error", "Full sync failed", error=str(exc))
            # DB unchanged — return stale data if available
            if last_sync is not None:
                dataset = self._db.get_full_dataset(board_ids)
                return SyncResult(
                    success=True,
                    stale_fallback=True,
                    synced_at=last_sync["synced_at"],
                    latest_updated_at=last_sync["latest_updated_at"],
                    boards_data=dataset,
                    board_count=len(dataset),
                    item_count=self._db.get_item_count(board_ids),
                    error_message=f"Sync failed: {exc}; returning cached data",
                )
            return SyncResult(
                success=False,
                error_code=exc.error_code,
                error_message=str(exc),
            )

        except Exception as exc:
            log_with_context(logger, "error", "Unexpected sync error", error=str(exc))
            if last_sync is not None:
                dataset = self._db.get_full_dataset(board_ids)
                return SyncResult(
                    success=True,
                    stale_fallback=True,
                    synced_at=last_sync["synced_at"],
                    latest_updated_at=last_sync["latest_updated_at"],
                    boards_data=dataset,
                    board_count=len(dataset),
                    item_count=self._db.get_item_count(board_ids),
                    error_message=f"Unexpected error: {exc}; returning cached data",
                )
            return SyncResult(
                success=False,
                error_code="INTERNAL_ERROR",
                error_message=f"Internal error: {exc}",
            )


# ---------------------------------------------------------------------------
# Singleton
# ---------------------------------------------------------------------------

_sync_engine: Optional[SyncEngine] = None


def init_sync_engine(
    client: MondayClient,
    db: SyncDB,
    max_age_seconds: int = 86400,
    items_page_size: int = 500,
    max_sync_wait_seconds: int = _DEFAULT_LOCK_TIMEOUT,
) -> SyncEngine:
    """Create and register the global SyncEngine singleton."""
    global _sync_engine
    _sync_engine = SyncEngine(
        client=client,
        db=db,
        max_age_seconds=max_age_seconds,
        items_page_size=items_page_size,
        max_sync_wait_seconds=max_sync_wait_seconds,
    )
    return _sync_engine


def get_sync_engine() -> SyncEngine:
    """Return the global SyncEngine singleton. Raises RuntimeError if not initialised."""
    if _sync_engine is None:
        raise RuntimeError("SyncEngine not initialised. Call init_sync_engine() at startup.")
    return _sync_engine
