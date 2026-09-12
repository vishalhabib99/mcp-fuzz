"""A tiny real MCP server used to exercise mcp-fuzz's engine end-to-end.
Deliberately includes one tool of each kind mcp-fuzz should distinguish:
well-behaved, crashes on bad input, hangs, and a non-read-only tool that
should be skipped by default.

Written against the official SDK's current `MCPServer` API (`mcp>=2.0`,
where `FastMCP` was renamed from `mcp.server.fastmcp.FastMCP`) — verified
directly against the installed package, not assumed from older examples.
"""

from __future__ import annotations

import os
import tempfile
import time

from mcp.server.mcpserver import MCPServer
from mcp.types import ToolAnnotations

server = MCPServer("mcp-fuzz-fixture")

READ_ONLY = ToolAnnotations(read_only_hint=True)
NOT_READ_ONLY = ToolAnnotations(read_only_hint=False)


@server.tool(annotations=READ_ONLY)
def well_behaved(name: str, count: int = 1) -> str:
    """Echoes name count times. Validates its own inputs properly."""
    if not isinstance(name, str):
        raise ValueError("name must be a string")
    if not isinstance(count, int):
        raise ValueError("count must be an integer")
    return (name + " ") * count


@server.tool(annotations=READ_ONLY)
def crashes_on_bad_input(value: int) -> str:
    """Divides 100 by value. Crashes (unhandled exception) if value is missing or the wrong type."""
    # Deliberately no validation — a wrong-typed or missing `value` raises
    # an uncaught TypeError, simulating a real server that doesn't guard
    # its handler against a malformed call.
    return str(100 / value)


@server.tool(annotations=READ_ONLY)
def hangs_forever(value: str) -> str:
    """Never returns — simulates a server tool that hangs on certain input."""
    time.sleep(3600)
    return value


@server.tool(annotations=NOT_READ_ONLY)
def delete_everything(target: str) -> str:
    """A destructive tool that should be skipped by default."""
    return f"deleted {target}"


@server.tool(annotations=READ_ONLY)
def always_crashes(x: str) -> str:
    """Raises unconditionally, even on schema-valid input — the SDK's own
    exception handling turns this into a structured error, not a process
    crash (see kills_process below for that)."""
    raise RuntimeError("this tool always crashes")


@server.tool(annotations=READ_ONLY)
def kills_process(x: str) -> str:
    """os._exit terminates the process immediately, bypassing all Python
    exception handling — a real process crash, not an SDK-caught error,
    to verify mcp-fuzz's connection-death detection and reconnect."""
    import os

    os._exit(1)


@server.tool(annotations=READ_ONLY)
def slow_but_fine(value: str) -> str:
    """Sleeps briefly then returns normally — well under the timeout, so it
    should never be flagged as a crash or hang, only (relative to this
    fixture server's other near-instant tools) as unusually slow by the
    latency check."""
    time.sleep(1.2)
    return value


@server.tool(annotations=READ_ONLY)
def bloated_but_fine(value: str) -> str:
    """Returns a huge response instantly — not slow, not a crash, only
    (relative to this fixture server's other tiny-response tools) unusually
    large by the response-size check."""
    return value * 10000


_LOCK_PATH = os.path.join(tempfile.gettempdir(), "mcp_fuzz_fixture_concurrency_lock")


@server.tool(annotations=READ_ONLY)
def not_concurrency_safe(value: str) -> str:
    """Uses a naive create-exclusive lock file with no retry/queue handling
    to serialize access to a shared resource — a real, common (buggy)
    pattern. Fine when called once at a time; a second call landing while
    the first is still "holding" the file raises FileExistsError, exactly
    the kind of shared-state race the concurrency check exists to catch."""
    fh = open(_LOCK_PATH, "x")
    try:
        time.sleep(0.3)
        return value
    finally:
        fh.close()
        os.remove(_LOCK_PATH)


if __name__ == "__main__":
    server.run(transport="stdio")
