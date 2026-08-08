# Standardise mcp-monday-server-playground

## Top-Level Overview

Align `mcp-monday-server-playground` with the `mcp-standards` requirements and the
`mcp-maximo-server` reference implementation. The goal is:

1. Replace `fastmcp` with `chuk-mcp-runtime` (the standard runtime used by maximo)
2. Replace the custom `MCPServerBase` / `server.py` / `tools_registration.py` wiring with
   the `@mcp_tool()` decorator pattern
3. Rewrite tools as individual async modules with Pydantic I/O schemas and correlation-ID logging
4. Add a proper `MondayClient` wrapping `httpx` (async, with pooling + retry, matching `base_client.py`)
5. Add all missing devops scaffolding: `Makefile`, `Containerfile`, `config.yaml`,
   `.env.example`, `.editorconfig`, `.dockerignore`, `.yamllint.yaml`
6. Align `pyproject.toml`, `.ruff.toml`, `.pre-commit-config.yaml` to maximo standards
7. Add real unit tests
8. Add `CONTRIBUTING.md`

**Non-goals:** adding new Monday.com tools beyond those already in `tools_registration.py`.

---

## Sub-Tasks

---

### Sub-Task 1 — Devops Scaffolding

**Intent:** Add every file required by `mcp-standards` that is currently missing.

**Expected Outcomes:**
- `Makefile` with all required targets (`venv`, `activate`, `install`, `serve`, `test`, `test-cov`, `docs`, `sbom`, `lint`, `lint-check`, `podman`, `podman-run`, `podman-stop`, `podman-test`, `trivy`, `clean`, `validate`, `build`, `version`)
- `Containerfile` — three-stage UBI9 build identical in structure to maximo's
- `config.yaml` — SSE transport on port 8080, monday section with env-var placeholders
- `.env.example` — all `MCP_MONDAY_*` env vars documented
- `.editorconfig` — identical to maximo's
- `.dockerignore` — identical to maximo's (project-name adjusted)
- `.yamllint.yaml` — identical to maximo's
- Entry point in `Containerfile` CMD is `mcp-monday-server` (matches `[project.scripts]`)

**Todo List:**
- [ ] Write `Makefile` (PROJECT_NAME=mcp-monday-server, SRC_DIR=src/mcp_monday_server, port 8080)
- [ ] Write `Containerfile` (three-stage UBI9, Python 3.13, entry point `mcp-monday-server`)
- [ ] Write `config.yaml` (type: sse, host: 0.0.0.0, port: 8080, monday section)
- [ ] Write `.env.example` (MCP_MONDAY_API_KEY, MCP_MONDAY_WORKSPACE_URL, MCP_MONDAY_TIMEOUT, MCP_MONDAY_MAX_RETRIES, MCP_MONDAY_LOG_LEVEL, MCP_MONDAY_LOG_FORMAT, MCP_MONDAY_ENVIRONMENT)
- [ ] Write `.editorconfig`
- [ ] Write `.dockerignore`
- [ ] Write `.yamllint.yaml`

**Relevant Context:**
- `mcp-maximo-server/Makefile` — copy and s/maximo/monday/g; adjust port 8080, env var names
- `mcp-maximo-server/Containerfile` — copy and adjust project name, Python version (keep 3.13 to match `.python-version`)
- `mcp-maximo-server/.editorconfig`, `.dockerignore`, `.yamllint.yaml` — copy verbatim (project-neutral)

**Status:** `[ ] pending`

---

### Sub-Task 2 — pyproject.toml, .ruff.toml, .pre-commit-config.yaml

**Intent:** Align all tooling config to maximo standards.

**Expected Outcomes:**
- `pyproject.toml`: drop `fastmcp`, `monday` library; add `chuk-mcp-runtime`, `pydantic`, `pyyaml`, `certifi`; align `[tool.ruff]`, `[tool.mypy]`, `[tool.black]`, `[tool.isort]`, `[tool.pytest]`; add `pytest-cov`, `pytest-mock`, `mypy`, `handsdown`, `types-pyyaml` to dev deps; add `[tool.bandit]`
- `.ruff.toml`: replace current minimal config with maximo's full rule set (line-length 120, E/W/F/I/C/B/UP)
- `.pre-commit-config.yaml`: replace with maximo's full hook set (trailing-whitespace, ruff, bandit, mypy, interrogate, yamllint, markdownlint, shellcheck, hadolint)

**Todo List:**
- [ ] Rewrite `pyproject.toml`
- [ ] Rewrite `.ruff.toml`
- [ ] Rewrite `.pre-commit-config.yaml`

**Relevant Context:**
- `mcp-maximo-server/pyproject.toml` — reference for all tool sections; replace `mcp-maximo-server` with `mcp-monday-server` and update dependencies
- `mcp-maximo-server/.ruff.toml` — the separate `.ruff.toml` in maximo is minimal (just exclude list); full ruff config lives in `pyproject.toml [tool.ruff]`; consolidate into `pyproject.toml`
- `mcp-maximo-server/.pre-commit-config.yaml` — copy verbatim

**Status:** `[ ] pending`

---

### Sub-Task 3 — Core Infrastructure (config, logging, exceptions)

**Intent:** Add the same three infrastructure modules that maximo has, adapted for Monday.com.

**Expected Outcomes:**
- `src/mcp_monday_server/config.py` — `MondayConfig` (Pydantic, `SecretStr` for API key), `LoggingConfig`, `ServerConfig`, `load_config()`; env var prefix `MCP_MONDAY_`
- `src/mcp_monday_server/logging_config.py` — `JSONFormatter`, `TextFormatter`, `setup_logging()`, `get_logger()`, `set_correlation_id()`, `log_with_context()`; sensitive data redaction for `api_key`, `authorization`, `token`
- `src/mcp_monday_server/exceptions.py` — `MondayMCPError`, `AuthenticationError`, `TimeoutError`, `MondayAPIError` (mirrors maximo's exception hierarchy)
- `src/mcp_monday_server/__init__.py` — updated to export new symbols, remove old ones

**Todo List:**
- [ ] Write `config.py`
- [ ] Write `logging_config.py`
- [ ] Write `exceptions.py`
- [ ] Update `__init__.py`

**Relevant Context:**
- `mcp-maximo-server/src/mcp_maximo_server/config.py` — copy and replace `MCP_MAXIMO_` with `MCP_MONDAY_`; remove maximo-specific fields; keep `api_key` as `SecretStr`; base_url becomes workspace_url (optional, for constructing item URLs)
- `mcp-maximo-server/src/mcp_maximo_server/logging_config.py` — copy verbatim (project-neutral)
- `mcp-maximo-server/src/mcp_maximo_server/exceptions.py` — copy and replace `Maximo` with `Monday`

**Status:** `[ ] pending`

---

### Sub-Task 4 — Monday HTTP Client

**Intent:** Replace the `monday` library + `HttpxGraphQLClient` monkey-patch with a clean async `MondayClient` that mirrors `base_client.py`.

**Expected Outcomes:**
- `src/mcp_monday_server/clients/__init__.py`
- `src/mcp_monday_server/clients/base_client.py` — copy of maximo's `BaseHTTPClient` (project-neutral)
- `src/mcp_monday_server/clients/monday_client.py` — `MondayClient(BaseHTTPClient)` with:
  - `graphql(query: str) -> dict` — POST to `https://api.monday.com/v2` with Bearer auth
  - `get_monday_client() -> MondayClient` — singleton factory reading `MondayConfig`

**Todo List:**
- [ ] Create `src/mcp_monday_server/clients/` directory with `__init__.py`
- [ ] Write `clients/base_client.py` (copy from maximo, project-neutral)
- [ ] Write `clients/monday_client.py`

**Relevant Context:**
- `mcp-maximo-server/src/mcp_maximo_server/clients/base_client.py` — copy verbatim; it has no maximo-specific code
- Current `mcp_server_base.py`'s `HttpxGraphQLClient._send()` and `monday_query()` — the logic moves into `MondayClient.graphql()`
- Current `tools_registration.py` shows all GraphQL queries used — `MondayClient` needs only one method: `graphql(query)`

**Status:** `[ ] pending`

---

### Sub-Task 5 — Rewrite Tools as @mcp_tool() Modules

**Intent:** Replace the monolithic `tools_registration.py` + `mcp_server_base.py` pattern with individual async tool modules decorated with `@mcp_tool()` from `chuk-mcp-runtime`.

**Expected Outcomes:**
- Each existing tool becomes its own file under `src/mcp_monday_server/tools/`
- Pydantic `Input`/`Output` schemas in `tools/schemas.py`
- All tools are `async def` with correlation-ID logging and structured error returns
- `tools/__init__.py` imports all tools so `from . import tools` in `main.py` triggers registration
- Files to create:
  - `tools/schemas.py`
  - `tools/list_boards.py`
  - `tools/get_board_groups.py`
  - `tools/get_board_columns.py`
  - `tools/create_board.py`
  - `tools/create_board_group.py`
  - `tools/list_items_in_groups.py`
  - `tools/list_subitems_in_items.py`
  - `tools/create_item.py`
  - `tools/update_item.py`
  - `tools/create_update.py`
  - `tools/get_item_by_id.py`
  - `tools/get_item_updates.py`
  - `tools/move_item_to_group.py`
  - `tools/delete_item.py`
  - `tools/archive_item.py`
  - `tools/get_item_files.py`
  - `tools/get_update_files.py`
  - `tools/get_docs.py`
  - `tools/get_doc_content.py`
  - `tools/create_doc.py`
  - `tools/add_doc_block.py`
  - `tools/__init__.py`

**Todo List:**
- [ ] Write `tools/schemas.py` (Pydantic Input/Output for every tool)
- [ ] Write each tool module (copy GraphQL logic from current `tools_registration.py`; wrap in `@mcp_tool()`, `async def`, correlation-ID logging, structured error return)
- [ ] Write `tools/__init__.py`

**Relevant Context:**
- `mcp-maximo-server/src/mcp_maximo_server/tools/get_service_request.py` — canonical tool pattern
- Current `mcp-monday-server-playground/src/mcp_monday_server/tools/tools_registration.py` — all GraphQL query strings and return formatting to preserve
- `chuk_mcp_runtime.common.mcp_tool_decorator.mcp_tool` — the `@mcp_tool()` decorator; auto-discovers tools imported in `main.py`

**Status:** `[ ] pending`

---

### Sub-Task 6 — Rewrite main.py and Remove Old Files

**Intent:** Replace the `MCPServerBase` / `server.py` / `tools_registration.py` entrypoint with the maximo-style `main.py` that calls `chuk_mcp_runtime.entry.main`.

**Expected Outcomes:**
- `src/mcp_monday_server/main.py` — mirrors maximo's `main.py`; loads config, sets up logging, configures SSE/stdio from `config.yaml`, calls `runtime_main()`
- `src/mcp_monday_server/__init__.py` — updated exports
- Old files **deleted**: `mcp_server_base.py`, `server.py`, `tools/monday_tools.py`, `tools/tools_registration.py`

**Todo List:**
- [ ] Rewrite `main.py`
- [ ] Update `__init__.py`
- [ ] Delete `mcp_server_base.py`
- [ ] Delete `server.py`
- [ ] Delete `tools/monday_tools.py`
- [ ] Delete `tools/tools_registration.py`

**Relevant Context:**
- `mcp-maximo-server/src/mcp_maximo_server/main.py` — copy and replace maximo references with monday; auth check becomes: api_key must be set
- `mcp-monday-server-playground/src/mcp_monday_server/main.py` — current entrypoint to replace

**Status:** `[ ] pending`

---

### Sub-Task 7 — Tests and CONTRIBUTING.md

**Intent:** Add real unit tests and the `CONTRIBUTING.md` required by `mcp-standards`.

**Expected Outcomes:**
- `test/__init__.py` (already exists, keep)
- `test/test_config.py` — tests for `load_config()` (env var override, missing api_key raises)
- `test/test_tools.py` — tests for 2–3 tools with mocked `MondayClient`
- `CONTRIBUTING.md`

**Todo List:**
- [ ] Write `test/test_config.py`
- [ ] Write `test/test_tools.py`
- [ ] Write `CONTRIBUTING.md`

**Relevant Context:**
- `mcp-maximo-server/test/` — reference test structure
- `mcp-standards/README.md` — CONTRIBUTING.md content requirements
- `pytest-asyncio`, `pytest-mock` available in dev deps after Sub-Task 2

**Status:** `[ ] pending`

---

## File Deletion Summary (Sub-Task 6)

| File | Reason |
|---|---|
| `src/mcp_monday_server/mcp_server_base.py` | Replaced by `chuk-mcp-runtime` + config/logging modules |
| `src/mcp_monday_server/server.py` | No longer needed; runtime handles server lifecycle |
| `src/mcp_monday_server/tools/monday_tools.py` | Stub file with no real logic |
| `src/mcp_monday_server/tools/tools_registration.py` | Logic moves to individual tool modules |

## New File Structure After All Sub-Tasks

```
mcp-monday-server-playground/
├── Containerfile
├── Makefile
├── CONTRIBUTING.md
├── config.yaml
├── pyproject.toml
├── .env.example
├── .editorconfig
├── .dockerignore
├── .yamllint.yaml
├── .ruff.toml
├── .pre-commit-config.yaml
├── .gitignore
├── .python-version
├── README.md
├── src/
│   └── mcp_monday_server/
│       ├── __init__.py
│       ├── main.py
│       ├── config.py
│       ├── logging_config.py
│       ├── exceptions.py
│       ├── clients/
│       │   ├── __init__.py
│       │   ├── base_client.py
│       │   └── monday_client.py
│       └── tools/
│           ├── __init__.py
│           ├── schemas.py
│           ├── list_boards.py
│           ├── get_board_groups.py
│           ├── get_board_columns.py
│           ├── create_board.py
│           ├── create_board_group.py
│           ├── list_items_in_groups.py
│           ├── list_subitems_in_items.py
│           ├── create_item.py
│           ├── update_item.py
│           ├── create_update.py
│           ├── get_item_by_id.py
│           ├── get_item_updates.py
│           ├── move_item_to_group.py
│           ├── delete_item.py
│           ├── archive_item.py
│           ├── get_item_files.py
│           ├── get_update_files.py
│           ├── get_docs.py
│           ├── get_doc_content.py
│           ├── create_doc.py
│           └── add_doc_block.py
└── test/
    ├── __init__.py
    ├── test_config.py
    └── test_tools.py
```
