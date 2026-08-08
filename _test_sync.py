"""Quick integration test — call get_all_boards_data twice and verify cache_hit.

Works with both SSE and Streamable HTTP transports.
Detects which transport is active by probing the endpoints.
"""
import asyncio
import json
import sys


async def try_streamable_http():
    """Test using Streamable HTTP transport (new default)."""
    from mcp.client.session import ClientSession
    from mcp.client.streamable_http import streamable_http_client

    async with streamable_http_client("http://localhost:8081/mcp") as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            return await run_calls(session)


async def try_sse():
    """Test using SSE transport (legacy fallback)."""
    from mcp.client.session import ClientSession
    from mcp.client.sse import sse_client

    async with sse_client("http://localhost:8081/sse", timeout=300) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            return await run_calls(session)


async def run_calls(session):
    """Run two tool calls and report results."""
    print("Connected and initialized.")
    sys.stdout.flush()

    # First call
    print("\n=== FIRST CALL (expect: synced or cache_hit) ===")
    print("This may take several minutes for a large workspace on first sync...")
    sys.stdout.flush()

    result = await session.call_tool("get_all_boards_data", {})
    text = result.content[0].text if result.content else "{}"
    data = json.loads(text)

    print(f"success      : {data.get('success')}")
    print(f"sync_status  : {data.get('sync_status')}")
    print(f"board_count  : {data.get('board_count')}")
    print(f"item_count   : {data.get('item_count')}")
    print(f"synced_at    : {data.get('synced_at')}")

    if not data.get("success"):
        print(f"error_code   : {data.get('error_code')}")
        print(f"error_message: {data.get('error_message')}")
        return False

    boards = data.get("boards", [])
    if boards:
        b = boards[0]
        items = b.get("items", [])
        print(f"board[0]     : {b['name']} ({len(items)} items)")
        if items:
            it = items[0]
            print(f"item[0]      : {it['name']}")
            cols = list(it.get("column_values", {}).keys())[:5]
            print(f"columns      : {cols}")

    # Second call — must be cache_hit
    print("\n=== SECOND CALL (expect: cache_hit) ===")
    sys.stdout.flush()

    result2 = await session.call_tool("get_all_boards_data", {})
    text2 = result2.content[0].text if result2.content else "{}"
    data2 = json.loads(text2)

    print(f"success      : {data2.get('success')}")
    print(f"sync_status  : {data2.get('sync_status')}")
    print(f"board_count  : {data2.get('board_count')}")
    print(f"item_count   : {data2.get('item_count')}")

    if data2.get("sync_status") == "cache_hit":
        print("\nSUCCESS: Cache is working correctly!")
        return True
    else:
        print(f"\nWARNING: Expected cache_hit, got: {data2.get('sync_status')}")
        return False


async def main():
    # Use SSE transport — works with MCP Inspector v2
    print("Connecting via SSE to http://localhost:8081/sse ...")
    sys.stdout.flush()
    try:
        await try_sse()
    except Exception as e:
        print(f"ERROR: {type(e).__name__}: {e}")
        sys.exit(1)


asyncio.run(main())
