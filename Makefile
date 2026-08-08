# Author: IBM Consulting Advantage
# Description: Makefile for the MCP Monday Server project
# Usage: make # this will print the help

# Project variables
PROJECT_NAME = mcp-monday-server
DOCS_DIR = docs
SRC_DIR = src/mcp_monday_server
TEST_DIR = test
DIRS_TO_CLEAN := __pycache__ .pytest_cache .tox .ruff_cache .pyre .mypy_cache .pytype

# Virtual environment configuration
VENVS_DIR := $(HOME)/.venv
VENV_DIR := $(VENVS_DIR)/$(PROJECT_NAME)

# =============================================================================
# 📖 DYNAMIC HELP
# =============================================================================
.PHONY: help
help:
	@grep "^# help\:" Makefile | sed 's/\# help\://'

# -----------------------------------------------------------------------------
# help: 🎯 MCP Monday Server
# help: (Production-grade MCP server for Monday.com integration)
# help:
# help: 🌱 VIRTUAL ENVIRONMENT & INSTALLATION
# help: venv            - Create a new virtual environment using uv venv
# help: activate        - Show command to activate the virtual environment
# help: install         - Install project dependencies using uv pip
# help: clean           - Clean up venv, build artifacts, and Python caches
# help:
# help: ▶️ RUN & TEST
# help: serve           - Run the MCP Monday Server
# help: test            - Run unit tests using pytest
# help: test-cov        - Run tests with coverage report
# help:
# help: 📚 DOCUMENTATION
# help: docs            - Generate project documentation using handsdown
# help: sbom            - Create software bill of materials (SBOM)
# help:
# help: 🔍 LINTING & FORMATTING
# help: lint            - Run ruff for linting and formatting
# help: lint-check      - Check code without making changes
# help:
# help: 🐳 CONTAINER BUILD & RUN
# help: podman          - Build the container image
# help: podman-run      - Run the container locally
# help: podman-stop     - Stop and remove the running container
# help: podman-test     - Test the running container
# help:
# help: 🛡️ SECURITY
# help: trivy           - Run Trivy scan for vulnerabilities
# -----------------------------------------------------------------------------

# =============================================================================
# 🌱 VIRTUAL ENVIRONMENT & INSTALLATION
# =============================================================================

.PHONY: venv
venv:
	@echo "🌱 Creating virtual environment..."
	@rm -rf "$(VENV_DIR)"
	@mkdir -p "$(VENVS_DIR)"
	@python3 -m venv "$(VENV_DIR)"
	@bash -c "source $(VENV_DIR)/bin/activate && python3 -m pip install --upgrade pip setuptools pdm uv"
	@echo -e "✅ Virtual environment ready:\n. $(VENV_DIR)/bin/activate"

.PHONY: activate
activate:
	@echo "To activate virtual environment:"
	@echo ". $(VENV_DIR)/bin/activate"

.PHONY: install
install: venv
	@echo "📦 Installing dependencies..."
	@bash -c "source $(VENV_DIR)/bin/activate && python3 -m uv pip install -e .[dev]"
	@echo "✅ Installation complete"

# =============================================================================
# ▶️ RUN & TEST
# =============================================================================

.PHONY: serve
serve:
	@echo "🚀 Starting MCP Monday Server..."
	@mcp-monday-server

.PHONY: test
test:
	@echo "🧪 Running tests..."
	@uv run -m pytest --maxfail=1 --disable-warnings -q

.PHONY: test-cov
test-cov:
	@echo "🧪 Running tests with coverage..."
	@uv run -m pytest --cov=$(SRC_DIR) --cov-report=html --cov-report=term

# =============================================================================
# 📚 DOCUMENTATION
# =============================================================================

.PHONY: docs
docs:
	@echo "📚 Generating documentation..."
	@handsdown --external https://github.ibm.com/advantage-mcp/monday -o $(DOCS_DIR) $(SRC_DIR)
	@echo "✅ Documentation generated in $(DOCS_DIR)/"

.PHONY: sbom
sbom:
	@echo "📋 Creating SBOM..."
	@pip-licenses --format=markdown --output-file=SBOM.md
	@echo "✅ SBOM created: SBOM.md"

# =============================================================================
# 🔍 LINTING & FORMATTING
# =============================================================================

.PHONY: lint
lint:
	@echo "🔍 Running ruff linter and formatter..."
	@ruff check $(SRC_DIR) --fix
	@ruff format $(SRC_DIR)
	@echo "✅ Linting complete"

.PHONY: lint-check
lint-check:
	@echo "🔍 Checking code style..."
	@ruff check $(SRC_DIR)
	@ruff format $(SRC_DIR) --check
	@echo "✅ Code style check complete"

# =============================================================================
# 🐳 CONTAINER BUILD & RUN
# =============================================================================

.PHONY: podman
podman:
	@echo "🐳 Building container image..."
	@podman build \
		--build-arg BUILD_DATE=$$(date -u +"%Y-%m-%dT%H:%M:%SZ") \
		--build-arg GIT_COMMIT=$$(git rev-parse HEAD 2>/dev/null || echo "unknown") \
		-t $(PROJECT_NAME):latest -f Containerfile .
	@echo "✅ Container built: $(PROJECT_NAME):latest"

.PHONY: podman-run
podman-run:
	@echo "🚀 Running container..."
	@-podman stop $(PROJECT_NAME) 2>/dev/null || true
	@-podman rm $(PROJECT_NAME) 2>/dev/null || true
	@podman run --name $(PROJECT_NAME) \
		-e MCP_MONDAY_API_KEY="$${MCP_MONDAY_API_KEY}" \
		-e MCP_MONDAY_WORKSPACE_URL="$${MCP_MONDAY_WORKSPACE_URL}" \
		-p 8080:8080 \
		--rm -d $(PROJECT_NAME):latest
	@echo "✅ Container running: $(PROJECT_NAME)"
	@echo "📋 View logs: podman logs -f $(PROJECT_NAME)"

.PHONY: podman-stop
podman-stop:
	@echo "🛑 Stopping and removing container..."
	@-podman stop $(PROJECT_NAME)
	@-podman rm $(PROJECT_NAME)
	@echo "✅ Container stopped"

.PHONY: podman-test
podman-test:
	@echo "🧪 Testing container..."
	@curl -sf --max-time 5 http://localhost:8080/health && echo "✅ Container is healthy" || echo "❌ Container health check failed"

# =============================================================================
# 🧹 CLEANUP
# =============================================================================

.PHONY: clean
clean:
	@echo "🧹 Cleaning up..."
	@rm -rf $(VENV_DIR) dist/ build/ *.egg-info $(DOCS_DIR)/ SBOM.md
	@for dir in $(DIRS_TO_CLEAN); do find . -type d -name $$dir -exec rm -rf {} + 2>/dev/null || true; done
	@find . -type f -name "*.pyc" -delete
	@find . -type f -name "*.pyo" -delete
	@find . -type f -name "*~" -delete
	@echo "✅ Clean complete"

# =============================================================================
# 🛡️ SECURITY
# =============================================================================

.PHONY: trivy
trivy:
	@echo "🛡️  Scanning container with Trivy..."
	@trivy image $(PROJECT_NAME):latest
	@echo "✅ Security scan complete"

# =============================================================================
# 🔧 DEVELOPMENT HELPERS
# =============================================================================

.PHONY: dev-setup
dev-setup: venv install
	@echo "🔧 Development environment ready!"
	@echo "Run 'make activate' to activate the virtual environment"

.PHONY: check-env
check-env:
	@echo "🔍 Checking environment variables..."
	@bash -c 'if [ -z "$$MCP_MONDAY_API_KEY" ]; then echo "❌ MCP_MONDAY_API_KEY not set"; else echo "✅ MCP_MONDAY_API_KEY: configured"; fi'
	@bash -c 'if [ -z "$$MCP_MONDAY_WORKSPACE_URL" ]; then echo "⚠️  MCP_MONDAY_WORKSPACE_URL not set (item URLs will be incomplete)"; else echo "✅ MCP_MONDAY_WORKSPACE_URL: $$MCP_MONDAY_WORKSPACE_URL"; fi'

.PHONY: validate
validate: lint-check test
	@echo "✅ Validation complete - code is ready for commit"

# =============================================================================
# 📦 BUILD & RELEASE
# =============================================================================

.PHONY: build
build:
	@echo "📦 Building distribution packages..."
	@python3 -m build
	@echo "✅ Build complete: dist/"

.PHONY: version
version:
	@echo "📌 Current version:"
	@grep "^version" pyproject.toml | cut -d'"' -f2

# =============================================================================
# 🎯 QUICK COMMANDS
# =============================================================================

.PHONY: all
all: clean install lint test
	@echo "✅ All tasks complete"

.PHONY: quick-test
quick-test:
	@echo "⚡ Quick test run..."
	@pytest $(TEST_DIR) -x -v

.PHONY: watch
watch:
	@echo "👀 Watching for changes..."
	@pytest-watch $(TEST_DIR)

# Default target
.DEFAULT_GOAL := help

