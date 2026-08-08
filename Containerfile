# syntax=docker/dockerfile:1.4
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# mcp-monday-server
#
# Three-stage build:
#   deps    — third-party packages only (cached; rebuilt when pyproject.toml changes)
#   builder — adds app source and installs the package into the venv
#   runtime — minimal UBI9 image with only the venv
#
# Build:
#   docker buildx build \
#     --build-arg BUILD_DATE=$(date -u +"%Y-%m-%dT%H:%M:%SZ") \
#     --build-arg GIT_COMMIT=$(git rev-parse HEAD) \
#     --no-cache \
#     -t mcp-monday-server:0.2.0 .
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

ARG PYTHON_VERSION=3.11
ARG BASE_IMAGE=registry.access.redhat.com/ubi9/ubi-minimal:9.8-1785906621
ARG BUILD_DATE
ARG GIT_COMMIT


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Stage 1 — deps
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
FROM ${BASE_IMAGE} AS deps

ARG PYTHON_VERSION

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

COPY pyproject.toml ./

# FIX 1: Removed --mount=type=cache — Railway's BuildKit rejects cache mounts
# without a registry-scoped cacheKey prefix (not supported on Railway's builder).
RUN python3 -m venv /build/.venv \
    && /build/.venv/bin/pip install --upgrade pip setuptools wheel \
    && /build/.venv/bin/pip install \
        $(python3 -c "import tomllib; f=open('pyproject.toml','rb'); d=tomllib.load(f); f.close(); print(' '.join(d.get('project',{}).get('dependencies',[])))")


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Stage 2 — builder
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
FROM deps AS builder

COPY src/ ./src/

# FIX 1 (continued): Removed --mount=type=cache here too.
RUN /build/.venv/bin/pip install --no-deps .

RUN test -f /build/.venv/bin/mcp-monday-server \
    && echo "✓ mcp-monday-server entry point verified" \
    || { echo "✗ mcp-monday-server NOT found — check [project.scripts] in pyproject.toml"; exit 1; }

RUN for f in /build/.venv/bin/*; do \
        if [ -f "$f" ] && grep -q '/build/.venv' "$f"; then \
            sed -i 's|/build/.venv|/app/.venv|g' "$f"; \
        fi; \
    done

# FIX 2: Removed compileall + the entire .py stripping block.
# Stripping .py files and relying solely on .pyc works locally because the
# Python patch version in the builder matches the runtime exactly.
# On Railway the builder and runtime Python patch versions can differ by one
# minor step, causing .pyc magic-number mismatches and
# "No module named mcp_monday_server.main" at startup.
# Keeping .py source in the venv is safe, correct, and adds only ~2 MB.


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Stage 3 — runtime
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
FROM ${BASE_IMAGE} AS runtime

ARG PYTHON_VERSION
ARG BUILD_DATE
ARG GIT_COMMIT

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

# Install Python runtime + shadow-utils (provides runuser and useradd).
# shadow-utils is needed to create appuser and for the entrypoint to drop
# privileges from root to appuser at runtime.
RUN microdnf upgrade -y \
    && microdnf install -y \
        python${PYTHON_VERSION} \
        shadow-utils \
    && microdnf clean all \
    && rm -rf /var/cache/yum /var/cache/dnf

RUN update-alternatives --install /usr/bin/python3 python3 \
        /usr/bin/python${PYTHON_VERSION} 1

# Create named user 1001 so runuser can reference it by name.
RUN useradd -u 1001 -g 0 -M -d /app -s /sbin/nologin appuser

WORKDIR /app

COPY --from=builder /build/.venv /app/.venv
COPY config.yaml ./

# Copy the entrypoint script that fixes volume permissions at runtime.
COPY docker-entrypoint.sh /usr/local/bin/entrypoint.sh
RUN chmod +x /usr/local/bin/entrypoint.sh

# Pre-create all runtime directories with correct ownership.
# /mnt/data is the Railway volume mount point — the entrypoint will re-chown
# it at runtime after the volume is mounted.
RUN mkdir -p /tmp/chuk_mcp_artifacts /tmp/monday_sync /mnt/data \
    && chown -R 1001:0 /app /tmp/chuk_mcp_artifacts /tmp/monday_sync /mnt/data \
    && chmod -R g=u /app /tmp/chuk_mcp_artifacts /tmp/monday_sync /mnt/data

EXPOSE 8080

# No USER instruction — entrypoint runs as root to fix volume permissions,
# then drops to appuser (uid 1001) via runuser before starting the server.

ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    MCP_MONDAY_LOG_FORMAT=json \
    MCP_MONDAY_LOG_LEVEL=INFO \
    CONFIG_PATH=/app/config.yaml \
    CHUK_ARTIFACTS_DIR=/tmp/chuk_mcp_artifacts \
    MCP_MONDAY_SYNC_DB_PATH=/mnt/data/monday_sync.db

ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
