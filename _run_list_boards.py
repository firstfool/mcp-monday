"""One-off script: read .env, call list_boards, print JSON result."""
import asyncio
import json
import os
import sys
import warnings

warnings.filterwarnings("ignore")

# Parse .env and remap legacy key names → MCP_MONDAY_ prefix
with open(".env") as f:
    for line in f:
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        k = k.strip()
        v = v.strip().strip('"').strip("'")
        if k == "MONDAY_API_KEY":
            os.environ["MCP_MONDAY_API_KEY"] = v
        elif k == "MONDAY_WORKSPACE_URL":
            os.environ["MCP_MONDAY_WORKSPACE_URL"] = v
        else:
            os.environ.setdefault(k, v)

sys.path.insert(0, "src")
from mcp_monday_server.tools.list_boards import list_boards  # noqa: E402

result = asyncio.run(list_boards(limit=50))
print(json.dumps(result, indent=2))
