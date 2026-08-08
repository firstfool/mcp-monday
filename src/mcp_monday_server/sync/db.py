"""SQLite sync database for Monday.com board data.

Schema design:
- 5 tables: boards, groups, columns, items, sync_log
- sync_log has exactly one row (id=1) — the last successful sync metadata
- write_full_sync() replaces all data in a single atomic transaction
- WAL mode + NORMAL synchronous for safe, fast single-writer operation
- asyncio.Lock serialises concurrent writes from the single event loop
"""

import asyncio
import json
import logging
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from ..logging_config import get_logger, log_with_context

logger = get_logger(__name__)

# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

_DDL = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS boards (
    id    TEXT PRIMARY KEY,
    name  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS groups (
    id       TEXT NOT NULL,
    board_id TEXT NOT NULL REFERENCES boards(id) ON DELETE CASCADE,
    title    TEXT NOT NULL,
    PRIMARY KEY (id, board_id)
);

CREATE TABLE IF NOT EXISTS columns (
    id       TEXT NOT NULL,
    board_id TEXT NOT NULL REFERENCES boards(id) ON DELETE CASCADE,
    title    TEXT NOT NULL,
    col_type TEXT NOT NULL,
    PRIMARY KEY (id, board_id)
);

CREATE TABLE IF NOT EXISTS items (
    id            TEXT PRIMARY KEY,
    board_id      TEXT NOT NULL REFERENCES boards(id) ON DELETE CASCADE,
    group_id      TEXT NOT NULL,
    group_title   TEXT NOT NULL,
    name          TEXT NOT NULL,
    column_values TEXT NOT NULL DEFAULT '{}',
    updated_at    TEXT
);

CREATE TABLE IF NOT EXISTS sync_log (
    id                INTEGER PRIMARY KEY CHECK (id = 1),
    synced_at         TEXT NOT NULL,
    latest_updated_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_items_board   ON items(board_id);
CREATE INDEX IF NOT EXISTS idx_items_group   ON items(group_id);
CREATE INDEX IF NOT EXISTS idx_items_updated ON items(updated_at);
"""


class SyncDB:
    """
    Async-safe SQLite store for Monday.com workspace data.

    Writes are serialised with ``asyncio.Lock``.
    Reads are lock-free (SQLite WAL allows concurrent readers).
    """

    def __init__(self, db_path: str = "/tmp/monday_sync.db") -> None:
        self.db_path = db_path
        self._lock = asyncio.Lock()
        self._conn: Optional[sqlite3.Connection] = None

    # ── Lifecycle ──────────────────────────────────────────────────────────

    def open(self) -> None:
        """Open the connection and create schema if needed."""
        if self.db_path != ":memory:":
            Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)

        self._conn = sqlite3.connect(
            self.db_path,
            check_same_thread=False,
            isolation_level=None,  # manual transaction control
        )
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.executescript(_DDL)
        log_with_context(logger, "info", "SyncDB opened", db_path=self.db_path)

    def close(self) -> None:
        """Close the SQLite connection."""
        if self._conn:
            self._conn.close()
            self._conn = None

    def _require_open(self) -> sqlite3.Connection:
        if self._conn is None:
            raise RuntimeError("SyncDB is not open. Call open() first.")
        return self._conn

    # ── Read operations (lock-free) ────────────────────────────────────────

    def get_last_sync_info(self) -> Optional[Dict[str, Any]]:
        """
        Return the last successful sync metadata, or None if never synced.

        Returns:
            dict with keys: synced_at (ISO str), latest_updated_at (ISO str | None)
        """
        conn = self._require_open()
        row = conn.execute("SELECT synced_at, latest_updated_at FROM sync_log WHERE id = 1").fetchone()
        if row is None:
            return None
        return {"synced_at": row["synced_at"], "latest_updated_at": row["latest_updated_at"]}

    def get_db_age_seconds(self) -> Optional[float]:
        """
        Return seconds elapsed since the last successful sync, or None if never synced.
        """
        info = self.get_last_sync_info()
        if info is None:
            return None
        synced_at = datetime.fromisoformat(info["synced_at"].replace("Z", "+00:00"))
        now = datetime.now(tz=timezone.utc)
        return (now - synced_at).total_seconds()

    def get_full_dataset(self, board_ids: Optional[List[str]] = None) -> List[Dict[str, Any]]:
        """
        Return the full cached dataset as a list of board dicts.

        Each board dict:
        {
            "id": str,
            "name": str,
            "groups": [{"id": str, "title": str}, ...],
            "columns": [{"id": str, "title": str, "type": str}, ...],
            "items": [
                {
                    "id": str,
                    "name": str,
                    "group_id": str,
                    "group_title": str,
                    "column_values": {column_title: text_value, ...},
                    "updated_at": str | None
                },
                ...
            ]
        }

        Args:
            board_ids: Optional list of board IDs to filter. None = all boards.
        """
        conn = self._require_open()

        if board_ids:
            placeholders = ",".join("?" * len(board_ids))
            board_rows = conn.execute(
                f"SELECT id, name FROM boards WHERE id IN ({placeholders}) ORDER BY name",
                board_ids,
            ).fetchall()
        else:
            board_rows = conn.execute("SELECT id, name FROM boards ORDER BY name").fetchall()

        result = []
        for board_row in board_rows:
            bid = board_row["id"]

            groups = [
                {"id": r["id"], "title": r["title"]}
                for r in conn.execute(
                    "SELECT id, title FROM groups WHERE board_id = ? ORDER BY rowid", (bid,)
                ).fetchall()
            ]

            columns = [
                {"id": r["id"], "title": r["title"], "type": r["col_type"]}
                for r in conn.execute(
                    "SELECT id, title, col_type FROM columns WHERE board_id = ? ORDER BY rowid", (bid,)
                ).fetchall()
            ]

            items = []
            for item_row in conn.execute(
                "SELECT id, name, group_id, group_title, column_values, updated_at "
                "FROM items WHERE board_id = ? ORDER BY updated_at DESC",
                (bid,),
            ).fetchall():
                try:
                    col_vals = json.loads(item_row["column_values"])
                except (json.JSONDecodeError, TypeError):
                    col_vals = {}
                items.append({
                    "id": item_row["id"],
                    "name": item_row["name"],
                    "group_id": item_row["group_id"],
                    "group_title": item_row["group_title"],
                    "column_values": col_vals,
                    "updated_at": item_row["updated_at"],
                })

            result.append({
                "id": bid,
                "name": board_row["name"],
                "groups": groups,
                "columns": columns,
                "items": items,
            })

        return result

    def get_item_count(self, board_ids: Optional[List[str]] = None) -> int:
        """Return the total number of items stored, optionally filtered by board."""
        conn = self._require_open()
        if board_ids:
            placeholders = ",".join("?" * len(board_ids))
            row = conn.execute(
                f"SELECT COUNT(*) FROM items WHERE board_id IN ({placeholders})", board_ids
            ).fetchone()
        else:
            row = conn.execute("SELECT COUNT(*) FROM items").fetchone()
        return row[0] if row else 0

    # ── Write operations (lock-serialised) ────────────────────────────────

    async def write_full_sync(
        self,
        boards_data: List[Dict[str, Any]],
        synced_at: str,
        latest_updated_at: Optional[str],
    ) -> None:
        """
        Atomically replace all board/group/column/item data with a fresh sync.

        The entire replacement happens inside a single ``BEGIN IMMEDIATE … COMMIT``
        transaction. If anything fails, the database rolls back to its previous state.

        Args:
            boards_data: List of board dicts from ``SyncEngine.fetch_all_boards()``
            synced_at: ISO 8601 UTC timestamp of this sync
            latest_updated_at: ISO 8601 UTC timestamp of the most recently updated item,
                                or None if no items exist
        """
        conn = self._require_open()
        async with self._lock:
            try:
                conn.execute("BEGIN IMMEDIATE")

                # Clear all existing data (CASCADE handles child rows)
                conn.execute("DELETE FROM sync_log")
                conn.execute("DELETE FROM items")
                conn.execute("DELETE FROM columns")
                conn.execute("DELETE FROM groups")
                conn.execute("DELETE FROM boards")

                # Insert fresh data
                for board in boards_data:
                    conn.execute(
                        "INSERT INTO boards(id, name) VALUES(?, ?)",
                        (board["id"], board["name"]),
                    )
                    for grp in board.get("groups", []):
                        conn.execute(
                            "INSERT INTO groups(id, board_id, title) VALUES(?, ?, ?)",
                            (grp["id"], board["id"], grp["title"]),
                        )
                    for col in board.get("columns", []):
                        conn.execute(
                            "INSERT INTO columns(id, board_id, title, col_type) VALUES(?, ?, ?, ?)",
                            (col["id"], board["id"], col["title"], col.get("type", "")),
                        )
                    for item in board.get("items", []):
                        conn.execute(
                            "INSERT INTO items(id, board_id, group_id, group_title, name, column_values, updated_at) "
                            "VALUES(?, ?, ?, ?, ?, ?, ?)",
                            (
                                item["id"],
                                board["id"],
                                item["group_id"],
                                item["group_title"],
                                item["name"],
                                json.dumps(item.get("column_values", {})),
                                item.get("updated_at"),
                            ),
                        )

                # Write sync log
                conn.execute(
                    "INSERT INTO sync_log(id, synced_at, latest_updated_at) VALUES(1, ?, ?)",
                    (synced_at, latest_updated_at),
                )
                conn.execute("COMMIT")

                log_with_context(
                    logger, "info", "SyncDB write_full_sync committed",
                    synced_at=synced_at,
                    latest_updated_at=latest_updated_at,
                    board_count=len(boards_data),
                )

            except Exception:
                conn.execute("ROLLBACK")
                log_with_context(logger, "error", "SyncDB write_full_sync rolled back")
                raise


# ---------------------------------------------------------------------------
# Singleton
# ---------------------------------------------------------------------------

_sync_db: Optional[SyncDB] = None


def init_sync_db(db_path: str = "/tmp/monday_sync.db") -> SyncDB:
    """Create, open, and register the global SyncDB singleton."""
    global _sync_db
    db = SyncDB(db_path=db_path)
    db.open()
    _sync_db = db
    return db


def get_sync_db() -> SyncDB:
    """Return the global SyncDB singleton. Raises RuntimeError if not initialised."""
    if _sync_db is None:
        raise RuntimeError("SyncDB not initialised. Call init_sync_db() at startup.")
    return _sync_db
