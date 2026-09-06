"""Regression tests for classifying a protocol-level MCP error response.

Found via real dogfooding against `firecrawl-mcp-server`: it validates tool
arguments with zod and, on a bad-input call, has the SDK raise a well-formed
JSON-RPC error (code -32602 INVALID_PARAMS) rather than returning a
CallToolResult with isError=true content. The client SDK surfaces that
server-side rejection as a raised `MCPError`. Before this fix, `_call_with_
outcome`'s blanket `except Exception` treated that identically to a real
process crash — every one of firecrawl's 93 bad-input calls came back
"crash", scoring a false 0%/F on a server that was actually behaving
correctly. A completed JSON-RPC error round trip is the opposite of a
crash: the process is alive and the connection is healthy.

Uses a plain stand-in connection (not a real subprocess) since this is
purely about exception classification, not real transport behavior — see
test_engine.py for the real end-to-end subprocess tests.
"""

import asyncio

import pytest
from mcp.shared.exceptions import McpError as MCPError

from mcp_fuzz.engine import _call_with_outcome


class _StubConnection:
    def __init__(self, exc: Exception):
        self._exc = exc
        self.reconnected = False

    class _Session:
        def __init__(self, outer: "_StubConnection"):
            self._outer = outer

        async def call_tool(self, tool_name, arguments):
            raise self._outer._exc

    def __post_init__(self):
        pass

    @property
    def session(self):
        return self._Session(self)

    async def connect(self):
        self.reconnected = True


def _run(conn, case="wrong_type", property_name="value"):
    return asyncio.run(
        _call_with_outcome(conn, None, "some_tool", case, property_name, {}, timeout=1.0)
    )


def test_protocol_level_mcp_error_is_graceful_not_a_crash():
    conn = _StubConnection(MCPError(-32602, "Invalid params: value"))
    outcome = _run(conn)
    assert outcome.outcome == "graceful_error"
    assert "-32602" in outcome.detail
    assert conn.reconnected is False  # connection is still healthy, no need to reconnect


def test_protocol_level_mcp_error_on_valid_case_is_valid_call_errored():
    conn = _StubConnection(MCPError(-32602, "Invalid params"))
    outcome = _run(conn, case="valid", property_name=None)
    assert outcome.outcome == "valid_call_errored"


def test_missing_required_protocol_error_still_counts_as_bad_input_handled():
    conn = _StubConnection(MCPError(-32602, "value: Required"))
    outcome = _run(conn, case="missing_required")
    assert outcome.outcome == "graceful_error"


def test_non_protocol_exception_is_still_a_real_crash_and_triggers_reconnect():
    conn = _StubConnection(RuntimeError("boom"))
    outcome = _run(conn)
    assert outcome.outcome == "crash"
    assert conn.reconnected is True


def test_client_synthesized_connection_closed_is_still_a_real_crash():
    # CONNECTION_CLOSED (-32000) is raised by the client SDK itself when the
    # transport/process dies, not received from the server — must not be
    # swept into "graceful_error" just because it's shaped like an MCPError.
    conn = _StubConnection(MCPError(-32000, "Connection closed"))
    outcome = _run(conn)
    assert outcome.outcome == "crash"
    assert conn.reconnected is True


def test_client_synthesized_request_timeout_is_a_timeout_not_a_crash_or_graceful_error():
    # REQUEST_TIMEOUT (-32001) is the SDK's own internal-request-timeout
    # error, also synthesized locally rather than received from the server.
    conn = _StubConnection(MCPError(-32001, "Request timed out"))
    outcome = _run(conn)
    assert outcome.outcome == "timeout"
    assert conn.reconnected is True


def test_internal_error_wrapping_an_unhandled_exception_is_still_a_crash():
    # INTERNAL_ERROR (-32603) is what a framework commonly uses to wrap an
    # *unhandled exception from the tool's own business logic* so it
    # doesn't kill the whole process — not the server validating input and
    # rejecting it. An earlier version of this fix treated any non-
    # CONNECTION_CLOSED/-REQUEST_TIMEOUT MCPError as "graceful", which
    # would have silently turned a real finding on antvis/mcp-server-chart
    # (133 of 214 bad-input calls raising a raw internal TypeError wrapped
    # as exactly this shape) into a false 100%/A. Only INVALID_PARAMS
    # (-32602) is trusted as "properly handled"; everything else,
    # including this one, stays a crash.
    conn = _StubConnection(
        MCPError(-32603, "Failed to generate chart: Cannot read properties of null (reading 'map')")
    )
    outcome = _run(conn)
    assert outcome.outcome == "crash"
    assert conn.reconnected is True


def test_invalid_params_is_the_only_code_treated_as_graceful():
    conn = _StubConnection(MCPError(-32602, "Invalid input: expected string, received undefined"))
    outcome = _run(conn)
    assert outcome.outcome == "graceful_error"
    assert conn.reconnected is False
