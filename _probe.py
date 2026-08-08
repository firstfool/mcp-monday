"""Direct httpx probe — tests raw API connectivity."""
import httpx
import os

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

api_key = os.environ.get("MCP_MONDAY_API_KEY", "")
print(f"Key present: {bool(api_key)}, length: {len(api_key)}")

try:
    r = httpx.post(
        "https://api.monday.com/v2",
        headers={
            "Authorization": api_key,
            "Content-Type": "application/json",
            "API-Version": "2024-01",
        },
        json={"query": "{ boards(limit:5, page:1) { id name } }"},
        timeout=30.0,
    )
    print("Status:", r.status_code)
    print(r.text[:3000])
except Exception as e:
    print("Error:", type(e).__name__, str(e)[:500])
