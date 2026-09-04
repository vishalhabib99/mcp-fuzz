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


def _is_read_only(tool: types.Tool) -> bool:
    annotations = tool.annotations
    return bool(annotations is not None and annotations.read_only_hint is True)


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
    except TimeoutError:
        await conn.connect()
        return CallOutcome(case, property_name, "timeout", f"no response within {timeout}s")
    except Exception as exc:
        await conn.connect()
        return CallOutcome(case, property_name, "crash", f"{type(exc).__name__}: {exc}")

    if isinstance(result, types.CallToolResult) and result.is_error:
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
        schema = tool.input_schema

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
