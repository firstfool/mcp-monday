"""Tests for SyncDB, SyncEngine, get_all_boards_data tool, and config."""

import asyncio
import json
import os
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from mcp_monday_server.sync.db import SyncDB
from mcp_monday_server.sync.engine import SyncEngine, SyncResult


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture()
def db():
    """In-memory SyncDB, opened and closed around each test."""
    d = SyncDB(db_path=":memory:")
    d.open()
    yield d
    d.close()


@pytest.fixture()
def boards_data():
    """Minimal realistic board payload."""
    return [
        {
            "id": "1",
            "name": "Phoenix",
            "groups": [
                {"id": "g1", "title": "In Progress"},
                {"id": "g2", "title": "Done"},
            ],
            "columns": [
                {"id": "name", "title": "Name", "type": "text"},
                {"id": "status", "title": "Status", "type": "color"},
            ],
            "items": [
                {
                    "id": "101",
                    "name": "Fix login bug",
                    "group_id": "g1",
                    "group_title": "In Progress",
                    "column_values": {"Status": "Working on it"},
                    "updated_at": "2025-07-17T10:00:00Z",
                },
                {
                    "id": "102",
                    "name": "Deploy hotfix",
                    "group_id": "g2",
                    "group_title": "Done",
                    "column_values": {"Status": "Done"},
                    "updated_at": "2025-07-18T09:00:00Z",
                },
            ],
        },
        {
            "id": "2",
            "name": "Backlog",
            "groups": [{"id": "b1", "title": "Todo"}],
            "columns": [{"id": "name", "title": "Name", "type": "text"}],
            "items": [
                {
                    "id": "201",
                    "name": "New feature",
                    "group_id": "b1",
                    "group_title": "Todo",
                    "column_values": {},
                    "updated_at": "2025-07-16T08:00:00Z",
                }
            ],
        },
    ]


@pytest.fixture()
def mock_client():
    """MondayClient mock — graphql() is an AsyncMock."""
    client = MagicMock()
    client.graphql = AsyncMock()
    return client


# ===========================================================================
# SyncDB tests
# ===========================================================================

class TestSyncDBOpen:
    def test_requires_open_before_use(self):
        d = SyncDB(db_path=":memory:")
        with pytest.raises(RuntimeError):
            d.get_last_sync_info()

    def test_open_creates_schema(self, db):
        # Tables exist — no exception
        db._conn.execute("SELECT * FROM boards").fetchall()
        db._conn.execute("SELECT * FROM sync_log").fetchall()


class TestSyncDBLastSyncInfo:
    def test_returns_none_when_never_synced(self, db):
        assert db.get_last_sync_info() is None

    def test_returns_info_after_write(self, db, boards_data):
        asyncio.get_event_loop().run_until_complete(
            db.write_full_sync(boards_data, "2025-07-18T10:00:00Z", "2025-07-18T09:00:00Z")
        )
        info = db.get_last_sync_info()
        assert info is not None
        assert info["synced_at"] == "2025-07-18T10:00:00Z"
        assert info["latest_updated_at"] == "2025-07-18T09:00:00Z"

    def test_get_db_age_returns_none_when_never_synced(self, db):
        assert db.get_db_age_seconds() is None


class TestSyncDBWriteFullSync:
    @pytest.mark.asyncio
    async def test_full_sync_writes_all_tables(self, db, boards_data):
        await db.write_full_sync(boards_data, "2025-07-18T10:00:00Z", "2025-07-18T09:00:00Z")

        boards = db._conn.execute("SELECT id, name FROM boards ORDER BY id").fetchall()
        assert len(boards) == 2
        assert boards[0]["id"] == "1"

        items = db._conn.execute("SELECT id FROM items ORDER BY id").fetchall()
        assert len(items) == 3  # 2 Phoenix + 1 Backlog

    @pytest.mark.asyncio
    async def test_full_sync_replaces_previous_data(self, db, boards_data):
        await db.write_full_sync(boards_data, "2025-07-18T10:00:00Z", "2025-07-18T09:00:00Z")

        # Second sync with only one board
        await db.write_full_sync(
            [boards_data[0]], "2025-07-19T10:00:00Z", "2025-07-18T09:00:00Z"
        )

        boards = db._conn.execute("SELECT id FROM boards").fetchall()
        assert len(boards) == 1  # Backlog was replaced/removed

        items = db._conn.execute("SELECT id FROM items").fetchall()
        assert len(items) == 2  # only Phoenix items remain

    @pytest.mark.asyncio
    async def test_column_values_stored_as_json(self, db, boards_data):
        await db.write_full_sync(boards_data, "2025-07-18T10:00:00Z", None)
        row = db._conn.execute(
            "SELECT column_values FROM items WHERE id = '101'"
        ).fetchone()
        parsed = json.loads(row["column_values"])
        assert parsed == {"Status": "Working on it"}

    @pytest.mark.asyncio
    async def test_failed_sync_rolls_back(self, db):
        """If write_full_sync raises mid-transaction, DB stays intact."""
        # Seed initial data
        initial = [{"id": "1", "name": "Old", "groups": [], "columns": [], "items": []}]
        await db.write_full_sync(initial, "2025-07-17T10:00:00Z", None)

        # Corrupt the second sync to trigger a rollback
        bad_data = [{"id": "2", "name": "New", "groups": [], "columns": [],
                     "items": [{"id": None, "name": "bad", "group_id": "g1",
                                "group_title": "G", "column_values": {}, "updated_at": None}]}]
        with pytest.raises(Exception):
            await db.write_full_sync(bad_data, "2025-07-18T10:00:00Z", None)

        # Old board still intact
        boards = db._conn.execute("SELECT id FROM boards").fetchall()
        assert any(b["id"] == "1" for b in boards)


class TestSyncDBGetFullDataset:
    @pytest.mark.asyncio
    async def test_returns_all_boards(self, db, boards_data):
        await db.write_full_sync(boards_data, "2025-07-18T10:00:00Z", None)
        result = db.get_full_dataset()
        assert len(result) == 2
        names = {b["name"] for b in result}
        assert names == {"Phoenix", "Backlog"}

    @pytest.mark.asyncio
    async def test_filter_by_board_ids(self, db, boards_data):
        await db.write_full_sync(boards_data, "2025-07-18T10:00:00Z", None)
        result = db.get_full_dataset(board_ids=["1"])
        assert len(result) == 1
        assert result[0]["name"] == "Phoenix"

    @pytest.mark.asyncio
    async def test_items_have_flattened_column_values(self, db, boards_data):
        await db.write_full_sync(boards_data, "2025-07-18T10:00:00Z", None)
        phoenix = next(b for b in db.get_full_dataset() if b["id"] == "1")
        item = next(i for i in phoenix["items"] if i["id"] == "101")
        assert isinstance(item["column_values"], dict)
        assert item["column_values"]["Status"] == "Working on it"

    @pytest.mark.asyncio
    async def test_unknown_board_id_returns_empty(self, db, boards_data):
        await db.write_full_sync(boards_data, "2025-07-18T10:00:00Z", None)
        result = db.get_full_dataset(board_ids=["999"])
        assert result == []


# ===========================================================================
# SyncEngine tests
# ===========================================================================

class TestSyncEngineCacheHit:
    @pytest.mark.asyncio
    async def test_cache_hit_when_timestamps_match(self, db, boards_data, mock_client):
        # Pre-populate DB
        await db.write_full_sync(boards_data, "2025-07-18T10:00:00Z", "2025-07-18T09:00:00Z")

        # Monday returns same timestamp as stored
        mock_client.graphql.return_value = {
            "boards": [{"items_page": {"items": [{"id": "102", "updated_at": "2025-07-18T09:00:00Z"}]}}]
        }

        engine = SyncEngine(client=mock_client, db=db, max_age_seconds=86400)
        result = await engine.run()

        assert result.success is True
        assert result.cache_hit is True
        assert result.synced is False
        assert len(result.boards_data) == 2
        # Only 1 API call for change detection
        assert mock_client.graphql.call_count == 1

    @pytest.mark.asyncio
    async def test_concurrent_calls_with_stale_data_one_does_detection(self, db, boards_data, mock_client):
        """When lock is held and DB has data, second caller returns stale immediately."""
        await db.write_full_sync(boards_data, "2025-07-18T10:00:00Z", "2025-07-18T09:00:00Z")
        mock_client.graphql.return_value = {
            "boards": [{"items_page": {"items": [{"id": "102", "updated_at": "2025-07-18T09:00:00Z"}]}}]
        }

        engine = SyncEngine(client=mock_client, db=db, max_age_seconds=86400)
        # Fire two concurrent calls
        results = await asyncio.gather(engine.run(), engine.run())
        assert all(r.success for r in results)
        # At least one should be a cache hit or stale fallback
        assert any(r.cache_hit or r.stale_fallback for r in results)

    @pytest.mark.asyncio
    async def test_sync_in_progress_returned_when_cold_start_times_out(self, db, mock_client):
        """When DB is empty and lock times out, return SYNC_IN_PROGRESS."""
        # Simulate a very slow sync by holding the lock
        engine = SyncEngine(
            client=mock_client, db=db, max_age_seconds=86400,
            max_sync_wait_seconds=0,  # zero timeout — immediately times out
        )
        # Manually hold the lock to simulate an ongoing sync
        await engine._lock.acquire()
        try:
            result = await engine.run()
        finally:
            engine._lock.release()

        assert result.success is False
        assert result.sync_in_progress is True
        assert result.error_code == "SYNC_IN_PROGRESS"
        assert "retry" in result.error_message.lower()


class TestSyncEngineFullSync:
    @pytest.mark.asyncio
    async def test_full_sync_when_no_previous_data(self, db, boards_data, mock_client):
        """First call ever — DB is empty, must sync."""
        # graphql returns boards, then schema, then items for each board
        mock_client.graphql.side_effect = [
            # Board list (page 1)
            {"boards": [{"id": "1", "name": "Phoenix"}, {"id": "2", "name": "Backlog"}]},
            {"boards": []},  # page 2 (empty = stop)
            # Phoenix schema
            {"boards": [{"groups": [{"id": "g1", "title": "In Progress"}],
                         "columns": [{"id": "status", "title": "Status", "type": "color"}]}]},
            # Phoenix items page 1
            {"boards": [{"items_page": {"cursor": None,
                                        "items": [{"id": "101", "name": "Task",
                                                   "updated_at": "2025-07-18T09:00:00Z",
                                                   "group": {"id": "g1"},
                                                   "column_values": [{"id": "status", "title": "Status", "text": "Done"}]}]}}]},
            # Backlog schema
            {"boards": [{"groups": [{"id": "b1", "title": "Todo"}],
                         "columns": [{"id": "name", "title": "Name", "type": "text"}]}]},
            # Backlog items page 1
            {"boards": [{"items_page": {"cursor": None, "items": []}}]},
        ]

        engine = SyncEngine(client=mock_client, db=db, max_age_seconds=86400)
        result = await engine.run()

        assert result.success is True
        assert result.synced is True
        assert result.cache_hit is False
        assert result.board_count == 2
        assert result.item_count == 1

    @pytest.mark.asyncio
    async def test_full_sync_when_timestamp_changed(self, db, boards_data, mock_client):
        """Stored timestamp differs from Monday → triggers full sync."""
        await db.write_full_sync(boards_data, "2025-07-17T10:00:00Z", "2025-07-17T08:00:00Z")

        mock_client.graphql.side_effect = [
            # Change detection → newer timestamp
            {"boards": [{"items_page": {"items": [{"id": "102", "updated_at": "2025-07-18T09:00:00Z"}]}}]},
            # Board list
            {"boards": [{"id": "1", "name": "Phoenix"}]},
            {"boards": []},
            # Phoenix schema
            {"boards": [{"groups": [{"id": "g1", "title": "In Progress"}],
                         "columns": [{"id": "status", "title": "Status", "type": "color"}]}]},
            # Phoenix items
            {"boards": [{"items_page": {"cursor": None,
                                        "items": [{"id": "101", "name": "Task",
                                                   "updated_at": "2025-07-18T09:00:00Z",
                                                   "group": {"id": "g1"},
                                                   "column_values": []}]}}]},
        ]

        engine = SyncEngine(client=mock_client, db=db, max_age_seconds=86400)
        result = await engine.run()

        assert result.success is True
        assert result.synced is True
        assert result.latest_updated_at == "2025-07-18T09:00:00Z"

    @pytest.mark.asyncio
    async def test_force_refresh_bypasses_change_detection(self, db, boards_data, mock_client):
        await db.write_full_sync(boards_data, "2025-07-18T10:00:00Z", "2025-07-18T09:00:00Z")

        mock_client.graphql.side_effect = [
            # Board list (force=True skips detection call)
            {"boards": [{"id": "1", "name": "Phoenix"}]},
            {"boards": []},
            {"boards": [{"groups": [], "columns": []}]},
            {"boards": [{"items_page": {"cursor": None, "items": []}}]},
        ]

        engine = SyncEngine(client=mock_client, db=db, max_age_seconds=86400)
        result = await engine.run(force=True)

        assert result.success is True
        assert result.synced is True
        # No change detection call was made
        assert mock_client.graphql.call_count == 4

    @pytest.mark.asyncio
    async def test_stale_fallback_when_api_fails_and_db_has_data(
        self, db, boards_data, mock_client
    ):
        """If API call fails but DB has old data, return stale data with warning."""
        await db.write_full_sync(boards_data, "2025-07-17T10:00:00Z", "2025-07-17T08:00:00Z")

        from mcp_monday_server.exceptions import MondayAPIError
        mock_client.graphql.side_effect = MondayAPIError("Rate limit exceeded", status_code=429)

        engine = SyncEngine(client=mock_client, db=db, max_age_seconds=86400)
        result = await engine.run()

        assert result.success is True
        assert result.stale_fallback is True
        assert len(result.boards_data) == 2
        assert result.error_message is not None

    @pytest.mark.asyncio
    async def test_failure_with_no_db_returns_error(self, db, mock_client):
        """If API call fails AND DB has no data, return error."""
        from mcp_monday_server.exceptions import MondayAPIError
        mock_client.graphql.side_effect = MondayAPIError("Network error")

        engine = SyncEngine(client=mock_client, db=db, max_age_seconds=86400)
        result = await engine.run()

        assert result.success is False
        assert result.error_code == "API_ERROR"

    @pytest.mark.asyncio
    async def test_age_safety_valve_forces_sync(self, db, boards_data, mock_client):
        """DB older than max_age_seconds triggers sync even if no new items."""
        await db.write_full_sync(boards_data, "2025-07-01T10:00:00Z", "2025-07-01T08:00:00Z")

        mock_client.graphql.side_effect = [
            {"boards": [{"id": "1", "name": "Phoenix"}]},
            {"boards": []},
            {"boards": [{"groups": [], "columns": []}]},
            {"boards": [{"items_page": {"cursor": None, "items": []}}]},
        ]

        # max_age_seconds = 1 second — DB from July 1 is definitely stale
        engine = SyncEngine(client=mock_client, db=db, max_age_seconds=1)
        result = await engine.run()

        assert result.synced is True


# ===========================================================================
# get_all_boards_data tool tests
# ===========================================================================

class TestGetAllBoardsDataTool:
    @pytest.mark.asyncio
    async def test_success_cache_hit(self, boards_data):
        mock_engine = MagicMock()
        mock_engine.run = AsyncMock(return_value=SyncResult(
            success=True,
            cache_hit=True,
            synced_at="2025-07-18T10:00:00Z",
            latest_updated_at="2025-07-18T09:00:00Z",
            boards_data=boards_data,
            board_count=2,
            item_count=3,
        ))

        with patch("mcp_monday_server.tools.get_all_boards_data.get_sync_engine",
                   return_value=mock_engine):
            from mcp_monday_server.tools.get_all_boards_data import get_all_boards_data
            result = await get_all_boards_data()

        assert result["success"] is True
        assert result["sync_status"] == "cache_hit"
        assert result["board_count"] == 2
        assert result["item_count"] == 3
        assert len(result["boards"]) == 2

    @pytest.mark.asyncio
    async def test_success_synced(self, boards_data):
        mock_engine = MagicMock()
        mock_engine.run = AsyncMock(return_value=SyncResult(
            success=True,
            synced=True,
            synced_at="2025-07-18T10:00:00Z",
            latest_updated_at="2025-07-18T09:00:00Z",
            boards_data=boards_data,
            board_count=2,
            item_count=3,
        ))

        with patch("mcp_monday_server.tools.get_all_boards_data.get_sync_engine",
                   return_value=mock_engine):
            from mcp_monday_server.tools.get_all_boards_data import get_all_boards_data
            result = await get_all_boards_data()

        assert result["sync_status"] == "synced"

    @pytest.mark.asyncio
    async def test_stale_fallback_includes_warning(self, boards_data):
        mock_engine = MagicMock()
        mock_engine.run = AsyncMock(return_value=SyncResult(
            success=True,
            stale_fallback=True,
            synced_at="2025-07-17T10:00:00Z",
            latest_updated_at="2025-07-17T08:00:00Z",
            boards_data=boards_data,
            board_count=2,
            item_count=3,
            error_message="Sync failed: timeout; returning cached data",
        ))

        with patch("mcp_monday_server.tools.get_all_boards_data.get_sync_engine",
                   return_value=mock_engine):
            from mcp_monday_server.tools.get_all_boards_data import get_all_boards_data
            result = await get_all_boards_data()

        assert result["sync_status"] == "sync_failed_stale_data_returned"
        assert "warning" in result
        assert "cached data" in result["warning"]

    @pytest.mark.asyncio
    async def test_api_failure_no_cache_returns_error(self):
        mock_engine = MagicMock()
        mock_engine.run = AsyncMock(return_value=SyncResult(
            success=False,
            error_code="API_ERROR",
            error_message="Rate limit exceeded",
        ))

        with patch("mcp_monday_server.tools.get_all_boards_data.get_sync_engine",
                   return_value=mock_engine):
            from mcp_monday_server.tools.get_all_boards_data import get_all_boards_data
            result = await get_all_boards_data()

        assert result["success"] is False
        assert result["error_code"] == "API_ERROR"

    @pytest.mark.asyncio
    async def test_invalid_board_id_rejected(self):
        with patch("mcp_monday_server.tools.get_all_boards_data.get_sync_engine"):
            from mcp_monday_server.tools.get_all_boards_data import get_all_boards_data
            result = await get_all_boards_data(board_ids=["abc; DROP TABLE boards;--"])

        assert result["success"] is False
        assert result["error_code"] == "VALIDATION_ERROR"

    @pytest.mark.asyncio
    async def test_valid_board_ids_passed_to_engine(self):
        mock_engine = MagicMock()
        mock_engine.run = AsyncMock(return_value=SyncResult(
            success=True, cache_hit=True, synced_at="2025-07-18T10:00:00Z",
            boards_data=[], board_count=0, item_count=0,
        ))

        with patch("mcp_monday_server.tools.get_all_boards_data.get_sync_engine",
                   return_value=mock_engine):
            from mcp_monday_server.tools.get_all_boards_data import get_all_boards_data
            await get_all_boards_data(board_ids=["123456", "789012"])

        mock_engine.run.assert_called_once_with(
            board_ids=["123456", "789012"], force=False
        )

    @pytest.mark.asyncio
    async def test_force_refresh_passed_to_engine(self):
        mock_engine = MagicMock()
        mock_engine.run = AsyncMock(return_value=SyncResult(
            success=True, synced=True, synced_at="2025-07-18T10:00:00Z",
            boards_data=[], board_count=0, item_count=0,
        ))

        with patch("mcp_monday_server.tools.get_all_boards_data.get_sync_engine",
                   return_value=mock_engine):
            from mcp_monday_server.tools.get_all_boards_data import get_all_boards_data
            await get_all_boards_data(force_refresh=True)

        mock_engine.run.assert_called_once_with(board_ids=None, force=True)


# ===========================================================================
# Config tests
# ===========================================================================

class TestSyncConfig:
    def test_load_config_from_env(self, monkeypatch):
        monkeypatch.setenv("MCP_MONDAY_API_KEY", "test-key-123")
        monkeypatch.setenv("MCP_MONDAY_CONFIG_PATH", "nonexistent.yaml")

        from mcp_monday_server.config import load_config
        config = load_config()

        assert config.monday.get_api_key() == "test-key-123"
        assert config.sync.db_path == "/tmp/monday_sync.db"
        assert config.sync.max_age_hours == 24
        assert config.sync.items_page_size == 500

    def test_sync_env_overrides(self, monkeypatch):
        monkeypatch.setenv("MCP_MONDAY_API_KEY", "key")
        monkeypatch.setenv("MCP_MONDAY_CONFIG_PATH", "nonexistent.yaml")
        monkeypatch.setenv("MCP_MONDAY_SYNC_DB_PATH", "/data/sync.db")
        monkeypatch.setenv("MCP_MONDAY_SYNC_MAX_AGE_HOURS", "12")
        monkeypatch.setenv("MCP_MONDAY_SYNC_PAGE_SIZE", "250")

        from mcp_monday_server.config import load_config
        config = load_config()

        assert config.sync.db_path == "/data/sync.db"
        assert config.sync.max_age_hours == 12
        assert config.sync.items_page_size == 250

    def test_missing_api_key_raises(self, monkeypatch):
        monkeypatch.delenv("MCP_MONDAY_API_KEY", raising=False)
        monkeypatch.delenv("MONDAY_API_KEY", raising=False)
        monkeypatch.setenv("MCP_MONDAY_CONFIG_PATH", "nonexistent.yaml")

        from mcp_monday_server.config import load_config
        with pytest.raises(ValueError, match="MCP_MONDAY_API_KEY is required"):
            load_config()

    def test_api_key_not_in_repr(self, monkeypatch):
        monkeypatch.setenv("MCP_MONDAY_API_KEY", "super-secret-key")
        monkeypatch.setenv("MCP_MONDAY_CONFIG_PATH", "nonexistent.yaml")

        from mcp_monday_server.config import load_config
        config = load_config()
        assert "super-secret-key" not in repr(config.monday)
