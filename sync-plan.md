# Monday.com MCP Sync Server — Design Plan

## Top-Level Overview

Replace the 11 TTL-cached tools with **one single MCP tool** (`get_all_boards_data`) that:

1. Queries Monday.com for the most recently updated item across all boards (**1 API call**).
2. Compares that timestamp against the last value stored in a local SQLite database.
3. If unchanged → returns the full dataset from SQLite immediately (**0 more API calls**).
4. If changed → fetches all boards, groups, columns, and items; writes them to SQLite; returns the dataset.

The ICA assistant calls this one tool and receives the entire workspace in a single structured JSON response. The LLM then reasons over that data locally to answer user queries (e.g. "open cards from Phoenix board this month").

### What is removed
- All 11 individual read tools (`list_boards`, `get_board_groups`, `get_board_columns`, `list_items_in_groups`, `list_subitems_in_items`, `get_item_by_id`, `get_item_updates`, `get_item_files`, `get_update_files`, `get_docs`, `get_doc_content`)
- The TTL-based `cache/` layer (`store.py`, `keys.py`)
- All TTL-related config fields in `CacheConfig`

### What is kept (unchanged)
- `clients/base_client.py` — async httpx transport
- `clients/monday_client.py` — GraphQL wrapper
- `exceptions.py` — all exception types
- `logging_config.py` — structured logging + redaction
- `main.py` skeleton — transport wiring (updated to init sync DB instead of cache)
- `Containerfile`, `Makefile`, devops scaffolding

---

## Edge Cases and Security Decisions

### Change detection signal
- Use `updated_at` from the most recently modified item across **all boards combined**.
- Query: `{ boards(limit: 500) { items_page(limit: 1) { items { id updated_at } } } }`
- This is **1 API call** on every `get_all_boards_data` invocation.
- Edge case: a board with zero items returns an empty `items_page`. The query handles this by
  taking the MAX `updated_at` across all boards that do return items.
- Edge case: all boards are empty (new workspace) → `latest_updated_at` is `None`; treat as
  "never synced" and do a full fetch.
- Edge case: Monday returns `updated_at` as ISO 8601 with timezone (e.g. `2025-07-18T10:00:00Z`).
  Store as UTC ISO string; compare as strings (lexicographic ISO ordering is correct).

### Concurrency / double-sync race
- The server is single-process async (one event loop). An `asyncio.Lock` on the sync engine
  prevents two concurrent tool calls from both deciding to sync simultaneously.
- While a sync is in progress, subsequent calls wait on the lock and read the freshly written DB.

### Partial sync failure
- If the full fetch fails mid-way (e.g. timeout on board 12 of 30), the SQLite DB is **not**
  updated. The old data is served from the DB unchanged. The error is returned to the caller.
- Writes to SQLite are wrapped in a single `BEGIN … COMMIT` transaction per sync so the DB is
  never left in a partially-written state.
- The `sync_log` table is only updated after all boards are successfully written.

### Large workspaces / pagination
- Items are fetched using Monday's cursor-based `items_page` pagination (500 items/page).
- All pages for a board are fetched before moving to the next board.
- Each board's pages are fetched sequentially (not concurrently) to avoid hammering the API.

### API key security
- The API key is read from `MCP_MONDAY_API_KEY` env var (or legacy `MONDAY_API_KEY`).
- It is stored as `SecretStr` in `MondayConfig` and never logged (redacted by `logging_config.py`).
- The SQLite DB stores **only board/item data** — the API key is never written to disk.

### SQLite file permissions
- The DB file is created in `/tmp/monday_sync.db` by default (writable by non-root UID 1001).
- The Containerfile pre-creates `/tmp/monday_sync` directory with `chown 1001:0 / chmod g=u`.
- If a volume is mounted at `/data`, set `MCP_MONDAY_SYNC_DB_PATH=/data/monday_sync.db` for
  persistence across container restarts.
- WAL mode + `PRAGMA synchronous=NORMAL` — safe for single-writer, fast for reads.

### Output size / LLM token limits
- 30 boards × 1000 items × ~500 bytes/item ≈ **~15 MB of JSON**.
- This may exceed LLM context windows. The tool provides optional `board_ids` filter so the
  ICA assistant can request specific boards instead of all boards when needed.
- `column_values` are included as flat `{column_title: text_value}` dicts (not raw JSON blobs)
  to reduce token count and improve LLM readability.

### Stale data safety valve
- If the SQLite DB is older than `max_age_hours` (default 24 h, configurable), force a full
  re-sync regardless of the change-detection result. This handles the case where Monday's
  `updated_at` is unreliable for deletions (deleted items don't appear in `items_page`).
- Deleted items are handled by the full re-sync: the DB is fully replaced, not appended to.

### Input validation
- `board_ids` (optional filter) must be a list of numeric strings. Validated before use in
  GraphQL queries to prevent injection.
- GraphQL query strings are constructed with f-strings using validated IDs only. No user-supplied
  free text is interpolated into queries.

---

## Output Schema

```json
{
  "success": true,
  "sync_status": "cache_hit",
  "synced_at": "2025-07-18T10:00:00Z",
  "latest_updated_at": "2025-07-17T15:30:00Z",
  "board_count": 3,
  "item_count": 127,
  "boards": [
    {
      "id": "123456",
      "name": "Phoenix",
      "groups": [
        {"id": "g1", "title": "In Progress"},
        {"id": "g2", "title": "Done"}
      ],
      "columns": [
        {"id": "status", "title": "Status", "type": "color"},
        {"id": "date", "title": "Due Date", "type": "date"}
      ],
      "items": [
        {
          "id": "789",
          "name": "Fix login bug",
          "group_id": "g1",
          "group_title": "In Progress",
          "column_values": {
            "Status": "Working on it",
            "Due Date": "2025-07-25"
          },
          "updated_at": "2025-07-17T15:30:00Z"
        }
      ]
    }
  ]
}
```

`sync_status` is one of: `"cache_hit"`, `"synced"`, `"sync_failed_stale_data_returned"`.

---

## Sub-Tasks

---

### Sub-Task 1 — SQLite Sync Database

**Intent:** Create a dedicated sync database module that owns the SQLite schema, all read/write
operations, and the singleton lifecycle. This replaces the generic key-value `cache/store.py`.

**Expected Outcomes:**
- `src/mcp_monday_server/sync/db.py` — `SyncDB` class + `init_sync_db()` + `get_sync_db()` singleton
- `src/mcp_monday_server/sync/__init__.py`
- Schema with 5 tables: `boards`, `groups`, `columns`, `items`, `sync_log`
- Methods: `get_full_dataset()`, `write_full_sync()`, `get_last_sync_info()`, `get_db_age_seconds()`

**SQLite Schema:**
```sql
CREATE TABLE IF NOT EXISTS boards (
    id    TEXT PRIMARY KEY,
    name  TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS groups (
    id       TEXT NOT NULL,
    board_id TEXT NOT NULL REFERENCES boards(id),
    title    TEXT NOT NULL,
    PRIMARY KEY (id, board_id)
);
CREATE TABLE IF NOT EXISTS columns (
    id       TEXT NOT NULL,
    board_id TEXT NOT NULL REFERENCES boards(id),
    title    TEXT NOT NULL,
    type     TEXT NOT NULL,
    PRIMARY KEY (id, board_id)
);
CREATE TABLE IF NOT EXISTS items (
    id           TEXT PRIMARY KEY,
    board_id     TEXT NOT NULL REFERENCES boards(id),
    group_id     TEXT NOT NULL,
    group_title  TEXT NOT NULL,
    name         TEXT NOT NULL,
    column_values TEXT NOT NULL,   -- JSON: {column_title: text_value}
    updated_at   TEXT             -- ISO 8601 UTC
);
CREATE TABLE IF NOT EXISTS sync_log (
    id               INTEGER PRIMARY KEY CHECK (id = 1),  -- single row
    synced_at        TEXT NOT NULL,
    latest_updated_at TEXT           -- NULL if no items existed at sync time
);
CREATE INDEX IF NOT EXISTS idx_items_board ON items(board_id);
CREATE INDEX IF NOT EXISTS idx_items_group ON items(group_id);
CREATE INDEX IF NOT EXISTS idx_items_updated ON items(updated_at);
```

**Todo List:**
1. Create `src/mcp_monday_server/sync/` directory with `__init__.py`
2. Write `sync/db.py`:
   - `SyncDB.__init__(db_path)` — store path
   - `SyncDB.open()` — connect, WAL mode, create schema
   - `SyncDB.close()` — close connection
   - `SyncDB.get_last_sync_info()` → `dict | None` — reads `sync_log`
   - `SyncDB.get_db_age_seconds()` → `float | None` — seconds since last sync
   - `SyncDB.get_full_dataset(board_ids=None)` → `list[dict]` — assembles boards + groups + columns + items
   - `SyncDB.write_full_sync(boards_data, synced_at, latest_updated_at)` — atomic transaction replacing all tables
   - `init_sync_db(db_path)` → `SyncDB` — create + open singleton
   - `get_sync_db()` → `SyncDB` — return singleton

**Relevant Context:**
- `cache/store.py` — reference for SQLite connection pattern, WAL mode, asyncio.Lock
- DB path default: `/tmp/monday_sync.db`
- `write_full_sync` must use `BEGIN IMMEDIATE … COMMIT` to be atomic; if it fails, old data is intact

**Status:** `[ ] pending`

---

### Sub-Task 2 — Sync Engine

**Intent:** Create the change-detection and full-fetch logic as a standalone module separate from
the MCP tool layer. This makes it independently testable.

**Expected Outcomes:**
- `src/mcp_monday_server/sync/engine.py` — `SyncEngine` class
- `SyncEngine.get_latest_monday_updated_at(board_ids=None)` → `str | None` — 1 API call
- `SyncEngine.fetch_all_boards(board_ids=None)` → `list[dict]` — full paginated fetch
- `SyncEngine.run(board_ids=None, force=False)` → `SyncResult` — orchestrates detection + fetch
- `SyncResult` dataclass: `synced`, `cache_hit`, `synced_at`, `latest_updated_at`, `boards_data`, `error`

**Change detection logic in `SyncEngine.run()`:**
```
1. lock.acquire()
2. last = db.get_last_sync_info()
3. age  = db.get_db_age_seconds()
4. if last is None or age > max_age_seconds:
       → force full sync (stale safety valve)
5. else:
       monday_ts = get_latest_monday_updated_at()    # 1 API call
       if monday_ts == last["latest_updated_at"]:
           → cache_hit: return db.get_full_dataset()
       else:
           → full sync
6. fetch_all_boards() → write_full_sync()
7. lock.release()
```

**Full fetch logic in `fetch_all_boards()`:**
- Query 1: `{ boards(limit: 500, page: N) { id name } }` — paginate until empty
- For each board:
  - Query A: `{ boards(ids: ID) { groups { id title } columns { id title type } } }`
  - Query B+: `{ boards(ids: ID) { items_page(limit: 500, cursor: ...) { cursor items { id name updated_at column_values { id title text } } } } }` — cursor-paginate until `cursor` is null
- Flatten `column_values` to `{title: text}` dict, drop null/empty values

**Todo List:**
1. Write `sync/engine.py`:
   - `SyncResult` dataclass
   - `SyncEngine.__init__(client, db, max_age_seconds)` — store references
   - `SyncEngine._lock` — `asyncio.Lock()`
   - `SyncEngine.get_latest_monday_updated_at(board_ids)` — single lightweight query
   - `SyncEngine._fetch_board_schema(client, board_id)` → `{groups, columns}`
   - `SyncEngine._fetch_board_items(client, board_id, columns)` → `list[item_dict]`
   - `SyncEngine.fetch_all_boards(board_ids)` → `list[board_dict]`
   - `SyncEngine.run(board_ids, force)` → `SyncResult`

**Relevant Context:**
- `clients/monday_client.py` — `MondayClient.graphql(query)` is the only API surface needed
- Items page cursor: when Monday returns `cursor: null`, there are no more pages
- `column_values` text field: use `item["column_values"][i]["text"]` not `value` (value is raw JSON)
- Board fetch order matters for progress logging; fetch schema and items board-by-board sequentially

**Status:** `[ ] pending`

---

### Sub-Task 3 — Single MCP Tool

**Intent:** Expose exactly one `@mcp_tool()` function that the ICA assistant calls. It delegates
entirely to `SyncEngine.run()` and formats the result.

**Expected Outcomes:**
- `src/mcp_monday_server/tools/get_all_boards_data.py` — single tool file
- Tool signature: `get_all_boards_data(board_ids: Optional[List[str]] = None, force_refresh: bool = False)`
- Returns structured dict matching the output schema above

**Todo List:**
1. Write `tools/get_all_boards_data.py`:
   - `@mcp_tool()` decorator
   - Validate `board_ids` — each must be a non-empty numeric string; reject invalid
   - Call `get_sync_engine().run(board_ids=board_ids, force=force_refresh)`
   - Map `SyncResult` → output dict
   - On `SyncResult.error` → return `{"success": False, "error_code": ..., "error_message": ...}`
   - On success with stale data returned → set `sync_status = "sync_failed_stale_data_returned"`

**Relevant Context:**
- `chuk_mcp_runtime.common.mcp_tool_decorator.mcp_tool` — decorator pattern from existing tools
- `logging_config.log_with_context` — use for all log lines
- `board_ids` validation: `re.match(r'^\d+$', bid)` — numeric only, no injection possible

**Status:** `[ ] pending`

---

### Sub-Task 4 — Config + SyncEngine Singleton + main.py

**Intent:** Strip the TTL-heavy `CacheConfig` down to a lean `SyncConfig`, wire the `SyncDB`
and `SyncEngine` singletons into `main.py`, and expose a `get_sync_engine()` factory for tools.

**Expected Outcomes:**
- `config.py` — `CacheConfig` replaced by `SyncConfig` with: `db_path`, `max_age_hours`, `items_page_size`
- `sync/engine.py` — module-level `_sync_engine` singleton + `init_sync_engine()` + `get_sync_engine()`
- `main.py` — calls `init_sync_db()` then `init_sync_engine()` at startup

**`SyncConfig` fields:**
```python
db_path: str = "/tmp/monday_sync.db"          # MCP_MONDAY_SYNC_DB_PATH env override
max_age_hours: int = 24                        # force re-sync if DB older than this
items_page_size: int = 500                     # items per API page (max 500)
```

**Todo List:**
1. Replace `CacheConfig` in `config.py` with `SyncConfig`; update `load_config()` to read
   `sync:` block from YAML and `MCP_MONDAY_SYNC_*` env vars
2. Add `ServerConfig.sync: SyncConfig` field (replacing `cache: CacheConfig`)
3. Add `init_sync_engine()` and `get_sync_engine()` to `sync/engine.py`
4. Update `main.py`: call `init_sync_db(config.sync.db_path)`, then
   `init_sync_engine(client=get_monday_client(), db=get_sync_db(), max_age_seconds=config.sync.max_age_hours * 3600)`
5. Update `tools/__init__.py` to import only `get_all_boards_data`

**Relevant Context:**
- `main.py` currently calls `init_cache_store()`; that call becomes `init_sync_db()` + `init_sync_engine()`
- `monday_client.py` `get_monday_client()` already exists; reuse it
- Remove all `cache/` imports from `main.py`

**Status:** `[ ] pending`

---

### Sub-Task 5 — Delete Old Tools + Cache Layer + Update Tests

**Intent:** Remove all code that no longer belongs, and write a focused test suite that covers
the new sync layer without hitting the real API.

**Files to delete:**
- `src/mcp_monday_server/cache/` (entire directory: `store.py`, `keys.py`, `__init__.py`)
- `src/mcp_monday_server/tools/list_boards.py`
- `src/mcp_monday_server/tools/get_board_groups.py`
- `src/mcp_monday_server/tools/get_board_columns.py`
- `src/mcp_monday_server/tools/list_items_in_groups.py`
- `src/mcp_monday_server/tools/list_subitems_in_items.py`
- `src/mcp_monday_server/tools/get_item_by_id.py`
- `src/mcp_monday_server/tools/get_item_updates.py`
- `src/mcp_monday_server/tools/get_item_files.py`
- `src/mcp_monday_server/tools/get_update_files.py`
- `src/mcp_monday_server/tools/get_docs.py`
- `src/mcp_monday_server/tools/get_doc_content.py`
- `src/mcp_monday_server/tools/schemas.py` (no longer needed — schemas inline in tool)
- `test/test_cache.py`

**Tests to write in `test/test_sync.py`:**
- `TestSyncDB` — open/close, `write_full_sync` atomicity, `get_full_dataset` filter by board_ids, `get_db_age_seconds`
- `TestSyncEngine` — cache hit (timestamps match), full sync triggered (timestamps differ), force refresh, partial API failure leaves DB unchanged, empty workspace (no items), concurrent calls serialised by lock
- `TestGetAllBoardsDataTool` — success path, API error, invalid board_id format rejected

**Test for `test_config.py`:** update to use `SyncConfig` instead of `CacheConfig`.

**Status:** `[ ] pending`

---

### Sub-Task 6 — config.yaml + Containerfile + .env.example

**Intent:** Align all deployment files with the new `sync:` config block and new DB path.

**config.yaml changes:**
- Remove entire `cache:` block
- Add `sync:` block:
  ```yaml
  sync:
    db_path: "/tmp/monday_sync.db"
    max_age_hours: 24
    items_page_size: 500
  ```

**Containerfile changes:**
- Replace `/tmp/monday_cache` dir with `/tmp/monday_sync`
- Replace `MCP_MONDAY_CACHE_DB_PATH` env var with `MCP_MONDAY_SYNC_DB_PATH`

**.env.example changes:**
- Replace `MONDAY_WORKSPACE_URL` comment with `MCP_MONDAY_SYNC_DB_PATH` (optional override)

**Status:** `[ ] pending`

---

## Files After Completion

```
src/mcp_monday_server/
├── clients/
│   ├── __init__.py          (unchanged)
│   ├── base_client.py       (unchanged)
│   └── monday_client.py     (unchanged)
├── sync/
│   ├── __init__.py          (NEW)
│   ├── db.py                (NEW — SyncDB)
│   └── engine.py            (NEW — SyncEngine)
├── tools/
│   ├── __init__.py          (REPLACED — 1 tool only)
│   └── get_all_boards_data.py  (NEW)
├── config.py                (MODIFIED — SyncConfig replaces CacheConfig)
├── exceptions.py            (unchanged)
├── logging_config.py        (unchanged)
├── main.py                  (MODIFIED — init sync layer)
└── __init__.py              (unchanged)

test/
├── __init__.py
├── test_config.py           (MODIFIED — SyncConfig assertions)
└── test_sync.py             (NEW — SyncDB + SyncEngine + tool tests)
```
