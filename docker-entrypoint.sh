#!/bin/sh
set -e
# Fix Railway volume mount permissions at runtime.
# Railway mounts volumes as root — chown before dropping to appuser.
chown -R 1001:0 /mnt/data 2>/dev/null || true
chmod -R g=u /mnt/data 2>/dev/null || true
exec runuser -u appuser -- mcp-monday-server
