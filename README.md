---
tags:
  - needs_api_key_user
  - needs_network_access_outbound
---

# MCP Monday Server (v2)

A **production-grade, containerised MCP server** that gives any LLM assistant instant access to
the full Monday.com workspace with a single tool call — without hammering the Monday.com API on
every request.

Built with [`chuk-mcp-runtime`](https://pypi.org/project/chuk-mcp-runtime/) and following
[IBM Consulting Advantage MCP standards](https://github.ibm.com/advantage-mcp/mcp-standards).

---

## Architecture

```
┌─────────────────────────────────────────────────────────────────┐
│  LLM Assistant (IBM Consulting Advantage / MCP Inspector)       │
└────────────────────────┬────────────────────────────────────────┘
                         │  MCP Protocol (SSE transport)
                         │  tool: get_all_boards_data()
                         ▼
┌─────────────────────────────────────────────────────────────────┐
│  Container  mcp-monday-server:latest  :8081                     │
│                                                                  │
│  ┌──────────────────────────────────────────────────────────┐   │
│  │  chuk-mcp-runtime  (MCP protocol layer)                  │   │
│  │   └─ @mcp_tool  get_all_boards_data()                    │   │
│  └────────────────────────┬─────────────────────────────────┘   │
│                           │                                      │
│  ┌────────────────────────▼─────────────────────────────────┐   │
│  │  SyncEngine                                               │   │
│  │   1. asyncio.Lock  (one sync at a time)                  │   │
│  │   2. Change detection  (1 lightweight API call)          │   │
│  │   3. Cache hit?  →  return SQLite data immediately       │   │
│  │   4. Changed?    →  full fetch  →  atomic DB write       │   │
│  └──────────┬──────────────────────────┬────────────────────┘   │
│             │                          │                         │
│  ┌──────────▼──────────┐   ┌──────────▼──────────────────────┐  │
│  │  SyncDB (SQLite)    │   │  MondayClient (httpx)           │  │
│  │  /data/monday_sync  │   │  api.monday.com  v2025-01       │  │
│  │  .db  (persistent)  │   │  GraphQL  (paginated)           │  │
│  │                     │   │                                  │  │
│  │  boards  (66)       │   │  boards → groups + columns      │  │
│  │  groups             │   │  → items (cursor-paginated      │  │
│  │  columns            │   │    500/page)                    │  │
│  │  items  (6,446)     │   │                                  │  │
│  │  sync_log           │   └──────────────────────────────────┘  │
│  └─────────────────────┘                                         │
└─────────────────────────────────────────────────────────────────┘
                         │  volume mount
                         ▼
                ./data/monday_sync.db   (Windows host)
```

---

## How the Tool Works — Decision Flow

```
get_all_boards_data() called
         │
         ▼
  Acquire asyncio.Lock (timeout = 120s)
         │
    ┌────┴────┐
    │ timed   │ DB has data? ──Yes──▶ return stale_fallback
    │  out    │ DB empty?   ──Yes──▶ return SYNC_IN_PROGRESS
    └────┬────┘                       retry_after_seconds: 10
         │ acquired
         ▼
  last sync info in DB?
         │
    ┌────┴────┐
    │   No    │──────────────────────────────────▶ FULL SYNC
    └────┬────┘
         │ Yes
         ▼
  DB older than 7 days?
         │
    ┌────┴────┐
    │   Yes   │──────────────────────────────────▶ FULL SYNC
    └────┬────┘
         │ No
         ▼
  GET latest updated_at from Monday.com  ← 1 API call
         │
  matches stored value?
         │
    ┌────┴────┐              ┌──────────────────────────────┐
    │   Yes   │─── CACHE ──▶ │ return SQLite data instantly │
    │         │    HIT       │ sync_status: "cache_hit"     │
    └────┬────┘              │ 0 more API calls             │
         │ No                └──────────────────────────────┘
         ▼
  FULL SYNC
  ├─ fetch all board IDs       (1 paginated query)
  ├─ for each board:
  │   ├─ fetch groups + columns  (1 query)
  │   └─ fetch items, paginated  (N queries, 500/page)
  └─ atomic SQLite write (BEGIN IMMEDIATE … COMMIT)
         │
         ▼
  sync_status: "synced"
  board_count: 66  |  item_count: 6,446
```

---

## Response Schema

```json
{
  "success": true,
  "sync_status": "cache_hit | synced | sync_failed_stale_data_returned",
  "synced_at": "2026-08-08T11:59:04Z",
  "latest_updated_at": "2026-08-07T15:30:00Z",
  "board_count": 66,
  "item_count": 6446,
  "boards": [
    {
      "id": "123456",
      "name": "Agile - Roadmap",
      "groups": [{ "id": "g1", "title": "In Progress" }],
      "columns": [{ "id": "status", "title": "Status", "type": "color" }],
      "items": [
        {
          "id": "789",
          "name": "Seamless Integration play",
          "group_id": "g1",
          "group_title": "In Progress",
          "column_values": { "Status": "Working on it", "Owner": "Alice" },
          "updated_at": "2026-08-07T15:30:00Z"
        }
      ]
    }
  ]
}
```

---

## Performance

| Scenario | API Calls | Latency | Notes |
|---|---|---|---|
| Cold start (empty DB) | 1 + ~200 | ~150s | Fetches 66 boards, 6,446 items |
| Cache hit (data unchanged) | 1 | < 1s | One lightweight timestamp check |
| Data changed on Monday.com | 1 + ~200 | ~150s | Full re-sync |
| DB older than 7 days | 1 + ~200 | ~150s | Safety valve re-sync |
| Container restart (volume) | 1 | < 1s | DB survives restart |

---

## Retry Behaviour

On cold start, the first sync takes ~150 seconds. The tool handles this gracefully:

```
Call 1 → sync starts in background → SYNC_IN_PROGRESS or timeout
Call 2 → sync still running         → SYNC_IN_PROGRESS or timeout
Call 3 → sync complete              → success: true, sync_status: "synced"
Call 4+ → cache populated           → success: true, sync_status: "cache_hit"
```

The tool docstring instructs the LLM to retry when it sees `error_code: "SYNC_IN_PROGRESS"` or a
timeout. The `retry_after_seconds: 10` field tells it how long to wait.

In production, the persistent volume mount means the DB is always pre-populated on container
restart. Cold-start sync only happens once — on first deployment.

---

## Project Structure

```
mcp-monday-server-playground-v2/
├── Containerfile                    # 3-stage UBI9 build (deps → builder → runtime)
├── Makefile                         # Devops targets
├── config.yaml                      # Transport, timeouts, sync settings
├── pyproject.toml                   # Project + tool configuration (PEP 621)
├── .env.example                     # Environment variable template
├── data/monday_sync.db              # Persistent SQLite cache (volume mount)
├── _test_sync.py                    # Integration test — verifies synced + cache_hit
└── src/mcp_monday_server/
    ├── main.py                      # Entrypoint — loads config, starts runtime
    ├── config.py                    # SyncConfig, MondayConfig, load_config()
    ├── logging_config.py            # Structured JSON logging with redaction
    ├── exceptions.py                # Typed exception hierarchy
    ├── clients/
    │   ├── base_client.py           # Async httpx client with retry/backoff
    │   └── monday_client.py         # Monday.com GraphQL client, API v2025-01
    ├── sync/
    │   ├── db.py                    # SyncDB — SQLite store, WAL mode, atomic writes
    │   └── engine.py                # SyncEngine — change detection, full fetch, lock
    └── tools/
        └── get_all_boards_data.py   # Single MCP tool exposed to assistants
```

---

## Quick Start

### 1. Prerequisites

- Docker (or Podman)
- A Monday.com Personal API token
- Node.js 22 in WSL (for MCP Inspector — see [Testing with MCP Inspector](#testing-with-mcp-inspector))

### 2. Configure environment

```bash
cp .env.example .env
# Edit .env — set MCP_MONDAY_API_KEY at minimum
```

### 3. Build the container

Run from **Git Bash or WSL** (not PowerShell — `$()` subshells require bash):

```bash
docker buildx build \
  --build-arg BUILD_DATE=$(date -u +"%Y-%m-%dT%H:%M:%SZ") \
  --build-arg GIT_COMMIT=$(git rev-parse HEAD) \
  -t mcp-monday-server:latest -f Containerfile .
```

### 4. Run the container

**Foreground** (logs stream directly to terminal, `Ctrl+C` to stop):

```bash
docker run --name mcp-monday-server \
  --env-file .env \
  -v "$(pwd)/config.yaml:/app/config.yaml:ro" \
  -v "$(pwd)/data:/data" \
  -e MCP_MONDAY_SYNC_DB_PATH=/data/monday_sync.db \
  -p 8081:8080 \
  --rm \
  mcp-monday-server:latest
```

**Background** (detached):

```bash
docker run --name mcp-monday-server \
  --env-file .env \
  -v "$(pwd)/config.yaml:/app/config.yaml:ro" \
  -v "$(pwd)/data:/data" \
  -e MCP_MONDAY_SYNC_DB_PATH=/data/monday_sync.db \
  -p 8081:8080 \
  -d \
  mcp-monday-server:latest
```

**Watch logs** (when running detached):

```bash
docker logs -f mcp-monday-server
```

**Stop the container:**

```bash
docker stop mcp-monday-server
# If started without --rm:
docker stop mcp-monday-server && docker rm mcp-monday-server
```

### 5. Verify with the integration test

```bash
.venv/Scripts/python.exe _test_sync.py
```

---

## Testing with MCP Inspector

MCP Inspector lets you call the tool interactively from a browser UI against the running container.

### Prerequisites

- WSL with Node.js 22 (`nvm use 22`)
- Container already running on port 8081 (step 4 above)

> **Why WSL?** MCP Inspector is an npm package that runs in Node. The Windows host IP
> (`10.255.255.254`) must be used instead of `localhost` when connecting from WSL to a Docker
> container running on the Windows host.

### Step 1 — Start Inspector

In a WSL terminal:

```bash
# Ensure Node 22 is active
nvm use 22

# Start Inspector with an extended request timeout (cold-start sync takes ~150s)
MCP_REQUEST_TIMEOUT=300000 npx @modelcontextprotocol/inspector@2
```

Inspector prints a local URL, typically `http://localhost:6274`. Open it in your browser.

### Step 2 — Connect to the container

In the Inspector UI:

1. Set transport to **SSE**
2. Set URL to `http://10.255.255.254:8081/sse`
3. Click **Connect**

> `10.255.255.254` is the Windows host IP as seen from WSL. `localhost` will not reach the
> Docker container.

### Step 3 — Call the tool

1. Navigate to the **Tools** tab
2. Select `get_all_boards_data`
3. Click **Run**

**On first call** (cold start): the sync takes ~150 seconds. Inspector may show a timeout — this
is expected. The sync continues in the background. Run the tool again after ~10 seconds; it will
return `sync_status: "synced"` with the full dataset.

**On subsequent calls**: returns instantly with `sync_status: "cache_hit"`.

### Step 4 — Stop Inspector

Press `Ctrl+C` in the WSL terminal where Inspector is running.

---

## Environment Variables

| Variable | Default | Description |
|---|---|---|
| `MCP_MONDAY_API_KEY` | — | Monday.com Personal API token (**required**) |
| `MCP_MONDAY_WORKSPACE_URL` | — | Workspace base URL (optional, e.g. `https://mycompany.monday.com`) |
| `MCP_MONDAY_SYNC_DB_PATH` | `/tmp/monday_sync.db` | SQLite DB path — use `/data/monday_sync.db` for persistence |
| `MCP_MONDAY_SYNC_MAX_AGE_HOURS` | `168` | Force re-sync if DB is older than this many hours (default: weekly) |
| `MCP_MONDAY_TIMEOUT` | `30` | API request timeout in seconds |
| `MCP_MONDAY_MAX_RETRIES` | `3` | Max retry attempts on network error |
| `MCP_MONDAY_LOG_LEVEL` | `INFO` | Log level: `DEBUG`, `INFO`, `WARNING`, `ERROR` |
| `MCP_MONDAY_LOG_FORMAT` | `json` | Log format: `json` (production) or `text` (development) |
| `MCP_MONDAY_ENVIRONMENT` | `production` | Environment identifier |

---

## Known Issues & Fixes

| # | Error | Root Cause | Fix |
|---|---|---|---|
| 1 | Port 8080 already allocated | Rancher Desktop using port 8080 | Use `-p 8081:8080` |
| 2 | `Invalid format 'json' for '%' style` | `config.yaml` had `format: "json"` passed to `logging.Formatter` | Removed `format: "json"` from `config.yaml` |
| 3 | `'Server' object has no attribute 'list_tools'` | `mcp 2.0.0` dropped the `@server.list_tools()` decorator API | Pinned `mcp>=1.26.0,<2.0.0` in `pyproject.toml` |
| 4 | `Cannot query field "title" on type "ColumnValue"` | Monday.com removed `title` from `ColumnValue` in API `2024-10+` | Query only `id` + `text`; resolve title from board schema |
| 5 | API version mismatch | Client was sending `API-Version: 2024-01` | Bumped to `2025-01` in `monday_client.py` |
| 6 | `Tool timed out after 60.0s` | `chuk-mcp-runtime` has a 60s tool timeout by default | Added `tools: timeout: 300.0` to `config.yaml` |
| 7 | MCP Inspector `-32001: Request timed out` | Inspector v0.9.0 / v2 has a short hardcoded timeout | Use `MCP_REQUEST_TIMEOUT=300000`; re-run tool after cold-start sync completes |
| 8 | Inspector `Connecting…` forever | Inspector (WSL) using `localhost` which doesn't reach Windows Docker | Use Windows host IP `10.255.255.254:8081` in Inspector URL |

---

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md) for guidelines on filing issues and submitting pull requests.

---

## License

MIT License — see [LICENSE](LICENSE) for details.

---

*Made with IBM Bob*
