
# syntax=docker/dockerfile:1.4
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# mcp-monday-server
#
# Three-stage build:
#   deps    — third-party packages only (cached; rebuilt when pyproject.toml changes)
#   builder — adds app source, pre-compiles bytecode, strips dev artefacts
#   runtime — minimal UBI9 image with only the cleaned venv
#
# Build:
#   docker buildx build \
#     --build-arg BUILD_DATE=$(date -u +"%Y-%m-%dT%H:%M:%SZ") \
#     --build-arg GIT_COMMIT=$(git rev-parse HEAD) \
#     --no-cache \
#     -t mcp-monday-server:0.2.0 .
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

# ── Global build arguments ────────────────────────────────────────────────────
# Declared before the first FROM so they propagate to every stage.
# PYTHON_VERSION must match across all three stages — change it here only.
ARG PYTHON_VERSION=3.11
# Pinned to a fully-patched UBI9 minimal tag — 0 Critical/Important CVEs as of Aug 2026.
# Update this tag when a newer patch release is available.
ARG BASE_IMAGE=registry.access.redhat.com/ubi9/ubi-minimal:9.8-1785906621
ARG BUILD_DATE
ARG GIT_COMMIT


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Stage 1 — deps
# Installs only third-party dependencies declared in pyproject.toml.
# Application source is deliberately excluded so this entire stage is served
# from cache on every code-only change.
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
FROM ${BASE_IMAGE} AS deps

ARG PYTHON_VERSION

# gcc + python-devel are required only for C-extension packages (e.g. cryptography,
# pydantic-core). They are intentionally absent from the runtime stage.
# Upgrade all base packages first to pull in the latest OS-level CVE patches
# (glibc, gnutls, libxml2, krb5-libs, etc.) before layering build tools.
RUN microdnf upgrade -y \
    && microdnf install -y \
        python${PYTHON_VERSION} \
        python${PYTHON_VERSION}-devel \
        gcc \
        git \
    && microdnf clean all \
    && rm -rf /var/cache/yum /var/cache/dnf

RUN update-alternatives --install /usr/bin/python3 python3 \
        /usr/bin/python${PYTHON_VERSION} 1

WORKDIR /build

# Copy manifest only — source changes will not invalidate this cache layer.
COPY pyproject.toml ./

# BuildKit pip cache: downloaded wheels are reused across builds on the same host.
# The cache mount is never written into the image layer.
# NOTE: Python snippet is intentionally on one line — multiline $() blocks
# confuse the Dockerfile parser, which tries to read them as instructions.
RUN --mount=type=cache,id=pip-deps,target=/root/.cache/pip \
    python3 -m venv /build/.venv \
    && /build/.venv/bin/pip install --upgrade pip setuptools wheel \
    && /build/.venv/bin/pip install \
        $(python3 -c "import tomllib; f=open('pyproject.toml','rb'); d=tomllib.load(f); f.close(); print(' '.join(d.get('project',{}).get('dependencies',[])))")


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Stage 2 — builder
# Installs the application package on top of the pre-warmed venv.
# Runs only when src/ changes — all heavy dependency work is cached in stage 1.
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
FROM deps AS builder

COPY src/ ./src/

# --no-deps: all dependencies are already present from stage 1.
# Non-editable install: package is fully baked into the venv, no /build/src needed at runtime.
RUN --mount=type=cache,id=pip-builder,target=/root/.cache/pip \
    /build/.venv/bin/pip install --no-deps .

# Hard-fail the build immediately if the entry point is missing.
# Catches mismatches between [project.scripts] in pyproject.toml and the source.
RUN test -f /build/.venv/bin/mcp-monday-server \
    && echo "✓ mcp-monday-server entry point verified" \
    || { echo "✗ mcp-monday-server NOT found — check [project.scripts] in pyproject.toml"; exit 1; }

# Fix shebang paths: entry point scripts are generated with /build/.venv shebangs.
# Using a for loop — xargs is not available in UBI9 minimal.
RUN for f in /build/.venv/bin/*; do \
        if [ -f "$f" ] && grep -q '/build/.venv' "$f"; then \
            sed -i 's|/build/.venv|/app/.venv|g' "$f"; \
        fi; \
    done

# Pre-compile bytecode BEFORE cleanup so __pycache__ dirs are populated.
# Faster container startup: Python reads .pyc directly, skipping source compilation.
# -q suppresses per-file output; errors are still printed.
RUN /build/.venv/bin/python3 -m compileall -q /build/.venv/lib

# Strip dev artefacts AFTER compileall.
# We keep __pycache__ (contains the .pyc files we just compiled).
# We remove source .py files from installed packages only — not entry points.
RUN find /build/.venv/lib -name "*.py" ! -path "*/bin/*" -delete \
    && find /build/.venv \
        \( -type d -name "tests" \
        -o -type d -name "test" \
        -o -type f -name "*.pyo" \
        -o -type f -name "RECORD" \
        -o -type f -name "INSTALLER" \
        -o -type f -name "REQUESTED" \) \
        -exec rm -rf {} + 2>/dev/null; true \
    && /build/.venv/bin/pip uninstall -y pip setuptools wheel 2>/dev/null; true


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Stage 3 — runtime
# Minimal UBI9 image. Receives only the cleaned, compiled venv.
# No compiler, no build tools, no source files.
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
FROM ${BASE_IMAGE} AS runtime

# Re-declare ARGs so they are in scope for this stage.
ARG PYTHON_VERSION
ARG BUILD_DATE
ARG GIT_COMMIT

# OCI standard labels — consumed by Trivy, Snyk, Harbor, and enterprise registries.
# Kubernetes / OpenShift labels kept for platform compatibility.
LABEL maintainer="IBM Consulting Advantage <advantage@ibm.com>" \
      name="mcp-monday-server" \
      version="0.2.0" \
      io.k8s.description="Production-grade MCP server with SSE support for Monday.com integration" \
      io.k8s.display-name="MCP Monday Server" \
      io.openshift.tags="mcp,monday,boards,items,project-management" \
      org.opencontainers.image.title="MCP Monday Server" \
      org.opencontainers.image.description="Production-grade MCP server for Monday.com board and item management" \
      org.opencontainers.image.version="0.2.0" \
      org.opencontainers.image.vendor="IBM Consulting Advantage" \
      org.opencontainers.image.licenses="MIT" \
      org.opencontainers.image.source="https://github.ibm.com/advantage-mcp/monday" \
      org.opencontainers.image.created="${BUILD_DATE}" \
      org.opencontainers.image.revision="${GIT_COMMIT}"

# Upgrade all base packages first to pull in latest OS-level CVE patches,
# then install the Python runtime. curl-minimal ships pre-installed with ubi9-minimal.
RUN microdnf upgrade -y \
    && microdnf install -y \
        python${PYTHON_VERSION} \
    && microdnf clean all \
    && rm -rf /var/cache/yum /var/cache/dnf

RUN update-alternatives --install /usr/bin/python3 python3 \
        /usr/bin/python${PYTHON_VERSION} 1

WORKDIR /app

# Venv is self-contained: non-editable install + fixed shebangs mean no /build/src needed.
COPY --from=builder /build/.venv /app/.venv

# Only runtime file the application needs alongside the venv.
COPY config.yaml ./

# OpenShift-compatible permissions: group-writable so arbitrary UIDs work correctly.
# /tmp/monday_sync.db — SQLite sync file written by the non-root user at runtime.
RUN mkdir -p /tmp/chuk_mcp_artifacts /tmp/monday_sync \
    && chown -R 1001:0 /app /tmp/chuk_mcp_artifacts /tmp/monday_sync \
    && chmod -R g=u /app /tmp/chuk_mcp_artifacts /tmp/monday_sync

EXPOSE 8080

USER 1001

ENV PATH="/app/.venv/bin:$PATH" \
    # Flush stdout/stderr immediately — required for container log drivers
    PYTHONUNBUFFERED=1 \
    # Prevent .pyc writes into the container filesystem at runtime
    PYTHONDONTWRITEBYTECODE=1 \
    # Level 1: strips assert statements only.
    PYTHONOPTIMIZE=1 \
    MCP_MONDAY_LOG_FORMAT=json \
    MCP_MONDAY_LOG_LEVEL=INFO \
    CONFIG_PATH=/app/config.yaml \
    CHUK_ARTIFACTS_DIR=/tmp/chuk_mcp_artifacts \
    # SQLite sync DB — override with MCP_MONDAY_SYNC_DB_PATH at runtime.
    # For serverless (no persistent volume) set to :memory: so the container
    # syncs from Monday.com once on first call without needing a writable filesystem.
    MCP_MONDAY_SYNC_DB_PATH=/tmp/monday_sync.db

# curl-minimal is already present in ubi9-minimal — no extra install needed.
# -s: silent, -f: fail on HTTP 4xx/5xx, --max-time: hard timeout.
HEALTHCHECK --interval=30s --timeout=10s --start-period=5s --retries=3 \
    CMD curl -sf --max-time 5 http://localhost:8080/health || exit 1

CMD ["mcp-monday-server"]
