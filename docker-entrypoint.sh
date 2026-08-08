#!/bin/sh
set -e
# Fix Railway volume mount permissions at runtime then start the server.
chown -R 1001:0 /mnt/data 2>/dev/null || true
chmod -R g=u /mnt/data 2>/dev/null || true
exec mcp-monday-server
