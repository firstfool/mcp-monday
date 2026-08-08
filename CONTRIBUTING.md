# Contributing to MCP Monday Server

Thank you for your interest in contributing to the MCP Monday Server!
This guide covers how to report issues, submit pull requests, and maintain code quality.

---

## 📋 Table of Contents

- [Getting Started](#getting-started)
- [Reporting Issues](#reporting-issues)
- [Submitting Pull Requests](#submitting-pull-requests)
- [Code Standards](#code-standards)
- [Testing](#testing)
- [Review and Merge Process](#review-and-merge-process)

---

## Getting Started

```bash
# Clone the repository
git clone https://github.ibm.com/advantage-mcp/monday.git
cd monday

# Set up the development environment
make install

# Activate the virtual environment
. ~/.venv/mcp-monday-server/bin/activate

# Copy and fill in your environment variables
cp .env.example .env
```

---

## Reporting Issues

Before opening an issue:

1. Search existing issues to avoid duplicates.
2. Check the `README.md` and `CONTRIBUTING.md` for answers.

When filing a bug report, include:

- Python version (`python --version`)
- Operating system
- Steps to reproduce
- Expected vs actual behaviour
- Relevant log output (redact any API keys or credentials)

---

## Submitting Pull Requests

1. **Fork** the repository and create a feature branch:

   ```bash
   git checkout -b feature/your-feature-name
   ```

2. **Make your changes** following the code standards below.

3. **Run the full validation suite** before pushing:

   ```bash
   make validate   # lint-check + tests
   ```

4. **Push** your branch and open a Pull Request against `main`.

5. PRs must:
   - Pass all CI checks (lint, tests, type-check)
   - Include tests for any new or changed behaviour
   - Update `README.md` if new tools or env vars are added
   - Follow the existing code style

---

## Code Standards

This project follows the [`mcp-standards`](https://github.ibm.com/advantage-mcp/mcp-standards) guidelines.

### Style

- **Formatter**: `ruff format` (line length 120, Black-compatible)
- **Linter**: `ruff check` (E, W, F, I, C, B, UP rules)
- **Type hints**: required on all public functions and methods
- **Docstrings**: required on all modules, classes, and public functions

Run the linter and formatter:

```bash
make lint        # fix and format
make lint-check  # check only (CI mode)
```

### Tool pattern

Each MCP tool must:

- Live in its own file under `src/mcp_monday_server/tools/`
- Be decorated with `@mcp_tool()` from `chuk-mcp-runtime`
- Be an `async def` function
- Have Pydantic `Input`/`Output` schemas in `tools/schemas.py`
- Generate a `correlation_id` (UUID) and call `set_correlation_id()` at the start
- Log entry and exit with `log_with_context()`
- Catch `MondayMCPError` and `Exception` separately and return a structured error dict
- Be exported from `tools/__init__.py`

Reference implementation: [`tools/list_boards.py`](src/mcp_monday_server/tools/list_boards.py)

### Environment variables

All environment variables must use the `MCP_MONDAY_` prefix and be documented in `.env.example`.

---

## Testing

Tests live in `test/` and use `pytest` with `pytest-asyncio` and `pytest-mock`.

```bash
make test        # run all tests
make test-cov    # run with coverage report
```

Requirements for new tests:

- File named `test_*.py`
- Classes named `Test*`, functions named `test_*`
- Tool tests must mock `get_monday_client()` — never make real API calls
- Each tool should have at minimum: a success case, an API error case, and a validation/edge case

---

## Review and Merge Process

1. At least one reviewer must approve the PR.
2. All CI checks must pass.
3. Squash-merge is preferred to keep a clean history.
4. The PR author is responsible for resolving merge conflicts.

---

## 📜 License

By contributing, you agree that your contributions will be licensed under the [MIT License](LICENSE).

---

*Made with Bob*
