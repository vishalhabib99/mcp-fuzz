"""Connects to a real, running MCP server over stdio and calls each of its
tools with schema-derived inputs to see how it actually behaves — distinct
from static analysis (mcp-doctor), which never runs the code at all.

Safety: a tool that isn't explicitly annotated `readOnlyHint: true` is
skipped by default. This library has no way to know whether a "write"-shaped
tool's side effects are safe to trigger against whatever backend the target
server is actually configured against (a real database, a real inbox, a
real filesystem) — silently calling it during a fuzz pass would be reckless
regardless of how careful the input generation is. Pass
`include_destructive=True` to opt into testing everything, at the caller's
own risk.

Isolation: any call that raises, times out, or otherwise leaves the
transport in a bad state triggers a full reconnect (kill + relaunch the
server subprocess) before the next case runs, so one tool crashing the
server doesn't invalidate every result after it.
"""

from __future__ import annotations

import asyncio
from contextlib import AsyncExitStack
from dataclasses import dataclass, field

from mcp import ClientSession, StdioServerParameters, types
from mcp.client.stdio import stdio_client
from mcp.shared.exceptions import MCPError

# The client SDK also raises MCPError itself (not just for a real response
# received from the server) when the transport dies or an internal request
# timeout elapses — REQUEST_TIMEOUT means "no response was ever received".
# INVALID_PARAMS is the one code that reliably means "the server actually
# validated my arguments and rejected them" — every other code (including
# INTERNAL_ERROR, -32603, which frameworks commonly use to wrap an
# unhandled exception from the tool's own business logic without killing
# the process) is treated conservatively as a crash; see the classification
# logic in `_call_with_outcome` for why that line is drawn exactly there.
# Read via getattr with the known literal fallback rather than assumed, in
# case an older `mcp` doesn't expose them on `types` the same way (same
# defensive style as `_field` below, which exists because of a real
# cross-version break).
_REQUEST_TIMEOUT = getattr(types, "REQUEST_TIMEOUT", -32001)
_INVALID_PARAMS = getattr(types, "INVALID_PARAMS", -32602)

from mcp_fuzz.generator import (
    generate_valid_arguments,
    missing_required_variants,
    wrong_type_variants,
)

DEFAULT_TIMEOUT_SECONDS = 15.0


@dataclass
class CallOutcome:
    case: str  # "valid" | "missing_required" | "wrong_type"
    property_name: str | None
    outcome: str  # "ok" | "graceful_error" | "crash" | "timeout"
    detail: str = ""


@dataclass
class ToolResult:
    name: str
    tested: bool
    skip_reason: str | None = None
    outcomes: list[CallOutcome] = field(default_factory=list)


@dataclass
class FuzzReport:
    server_command: str
    tools: list[ToolResult] = field(default_factory=list)
    connect_error: str | None = None


class _ServerConnection:
    """One live stdio connection to the target server, reconnectable on
    demand after a crash/timeout without tearing down the whole fuzz run."""

    def __init__(self, params: StdioServerParameters):
        self._params = params
        self._stack: AsyncExitStack | None = None
        self.session: ClientSession | None = None

    async def connect(self) -> None:
        await self.close()
        stack = AsyncExitStack()
        try:
            read, write = await stack.enter_async_context(stdio_client(self._params))
            session = await stack.enter_async_context(ClientSession(read, write))
            await session.initialize()
        except BaseException:
            await stack.aclose()
            raise
        self._stack = stack
        self.session = session

    async def close(self) -> None:
        if self._stack is not None:
            try:
                await self._stack.aclose()
            except Exception:
                pass  # best-effort teardown of a possibly-already-dead process
        self._stack = None
        self.session = None


def _field(model, snake_name: str, camel_name: str):
    """Reads a pydantic model field whose attribute name differs across
    `mcp` SDK major versions: mcp<2.0 exposed several `types` fields under
    their raw camelCase wire name directly (`isError`, `inputSchema`,
    `readOnlyHint`, ...); mcp>=2.0 renamed them to snake_case
    (`is_error`, `input_schema`, `read_only_hint`, ...) with the camelCase
    kept only as a validation alias, not a readable attribute. Verified
    directly: installing a real target server (`arxiv-mcp-server`, which
    pins `mcp<2.0`) into the same environment as mcp-fuzz silently
    downgraded the shared `mcp` package and broke every hardcoded
    snake_case attribute access with an AttributeError. Since mcp-fuzz's
    own resolved `mcp` version is independent of whatever the target
    server uses, and either generation could end up installed here, try
    the current name first and fall back to the older one rather than
    assuming either."""
    if hasattr(model, snake_name):
        return getattr(model, snake_name)
    return getattr(model, camel_name)


def _is_read_only(tool: types.Tool) -> bool:
    annotations = tool.annotations
    if annotations is None:
        return False
    return _field(annotations, "read_only_hint", "readOnlyHint") is True


async def _call_with_outcome(
    conn: _ServerConnection,
    params: StdioServerParameters,
    tool_name: str,
    case: str,
    property_name: str | None,
    arguments: dict,
    timeout: float,
) -> CallOutcome:
    """Runs one tool call, classifying the result, and reconnects the shared
    connection afterward if the call left it unusable."""
    try:
        assert conn.session is not None
        result = await asyncio.wait_for(
            conn.session.call_tool(tool_name, arguments), timeout=timeout
        )
    except asyncio.TimeoutError:
        # `asyncio.wait_for` raises `asyncio.TimeoutError`. Python 3.11
        # unified that with the builtin `TimeoutError` (same class), but on
        # 3.10 they're still distinct — `except TimeoutError` alone misses
        # it there and this falls through to the generic crash handler
        # below, misclassifying a genuine timeout as a crash. Caught this
        # via CI running 3.10 (mcp itself requires >=3.10), not locally,
        # where dev happened to be on 3.11+.
        await conn.connect()
        return CallOutcome(case, property_name, "timeout", f"no response within {timeout}s")
    except MCPError as exc:
        if exc.code == _REQUEST_TIMEOUT:
            # Synthesized by the client SDK itself when its own internal
            # request timeout elapses — no response was ever received, so
            # despite arriving as an MCPError this is a timeout, not a
            # server response. Reconnect: a timed-out in-flight request can
            # still be pending server-side over the shared connection.
            await conn.connect()
            return CallOutcome(case, property_name, "timeout", f"no response within {timeout}s ({exc.message})")
        if exc.code == _INVALID_PARAMS:
            # The *only* MCPError code that means "the server actually
            # validated this call's arguments and rejected them properly"
            # (e.g. zod's "Invalid input: expected string, received
            # undefined"). A completed, well-formed rejection — not a
            # crash. Verified against firecrawl-mcp-server: every one of
            # its 93 bad-input calls raises exactly this shape.
            outcome = "valid_call_errored" if case == "valid" else "graceful_error"
            return CallOutcome(case, property_name, outcome, f"{type(exc).__name__} (code {exc.code}): {exc.message}")
        # Every other MCPError means either the transport/process died
        # (CONNECTION_CLOSED, synthesized locally by the client SDK — the
        # real-crash case, verified via the `kills_process` fixture, which
        # os._exit()s and produces exactly this shape) or the *server's
        # own business logic* threw an unhandled exception that some
        # framework wrapper merely stopped from killing the whole process
        # (typically INTERNAL_ERROR, -32603) — neither is the server
        # "behaving the way its schema and description claim", so both
        # count as a crash. Verified this distinction matters against a
        # real repo, not just in theory: antvis/mcp-server-chart wraps 133
        # of 214 bad-input calls' raw internal TypeErrors ("Cannot read
        # properties of null", "data.map is not a function") as -32603
        # responses — an earlier version of this fix treated *any*
        # non-CONNECTION_CLOSED/-REQUEST_TIMEOUT MCPError as graceful,
        # which silently turned those 133 genuine internal crashes into a
        # false 100%/A. Only INVALID_PARAMS is safe to trust as "properly
        # handled" — anything else is conservatively still a crash.
        await conn.connect()
        return CallOutcome(case, property_name, "crash", f"{type(exc).__name__} (code {exc.code}): {exc.message}")
    except Exception as exc:
        await conn.connect()
        return CallOutcome(case, property_name, "crash", f"{type(exc).__name__}: {exc}")

    if isinstance(result, types.CallToolResult) and _field(result, "is_error", "isError"):
        outcome = "graceful_error" if case != "valid" else "ok"
        # A "valid" call returning is_error is itself worth surfacing, but
        # it's a content-level finding, not a crash — record it as an error
        # outcome regardless of which case triggered it so the report shows
        # the true state rather than papering over a valid-call failure.
        if case == "valid":
            outcome = "valid_call_errored"
        text = "; ".join(
            c.text for c in result.content if isinstance(c, types.TextContent)
        )[:300]
        return CallOutcome(case, property_name, outcome, text)

    return CallOutcome(case, property_name, "ok")


async def run_fuzz(
    command: str,
    args: list[str] | None = None,
    env: dict[str, str] | None = None,
    cwd: str | None = None,
    include_destructive: bool = False,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
) -> FuzzReport:
    params = StdioServerParameters(command=command, args=args or [], env=env, cwd=cwd)
    server_label = " ".join([command, *(args or [])])
    report = FuzzReport(server_command=server_label)

    conn = _ServerConnection(params)
    try:
        await conn.connect()
    except Exception as exc:
        report.connect_error = f"{type(exc).__name__}: {exc}"
        return report

    try:
        assert conn.session is not None
        tools_result = await conn.session.list_tools()
    except Exception as exc:
        report.connect_error = f"failed to list tools: {type(exc).__name__}: {exc}"
        await conn.close()
        return report

    for tool in tools_result.tools:
        if not include_destructive and not _is_read_only(tool):
            report.tools.append(ToolResult(
                name=tool.name,
                tested=False,
                skip_reason="not annotated readOnlyHint=true (use include_destructive to test anyway)",
            ))
            continue

        result = ToolResult(name=tool.name, tested=True)
        schema = _field(tool, "input_schema", "inputSchema")

        valid_args = generate_valid_arguments(schema)
        result.outcomes.append(
            await _call_with_outcome(conn, params, tool.name, "valid", None, valid_args, timeout)
        )

        for prop_name, args in missing_required_variants(schema):
            result.outcomes.append(
                await _call_with_outcome(conn, params, tool.name, "missing_required", prop_name, args, timeout)
            )

        for prop_name, args in wrong_type_variants(schema):
            result.outcomes.append(
                await _call_with_outcome(conn, params, tool.name, "wrong_type", prop_name, args, timeout)
            )

        report.tools.append(result)

    await conn.close()
    return report
