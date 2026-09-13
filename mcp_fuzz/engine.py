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
import time
from contextlib import AsyncExitStack
from dataclasses import dataclass, field

from mcp import ClientSession, StdioServerParameters, types
from mcp.client.stdio import get_default_environment, stdio_client

try:
    # mcp>=2.0 names this MCPError (all-caps); mcp==1.0.0's wheel only has
    # McpError — verified directly by inspecting both wheels, no back-compat
    # alias either direction. Two independent fixes on `main` each hardcoded
    # one name based on whichever version happened to resolve locally,
    # passed in that environment, and ImportError'd in the other — the exact
    # class of break `_field` below already exists to guard against. Try the
    # current name first, fall back to the older one, same as `_field`.
    from mcp.shared.exceptions import MCPError
except ImportError:
    from mcp.shared.exceptions import McpError as MCPError

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
from mcp_fuzz.sequence import extract_id, find_id_property, group_resource_tools

DEFAULT_TIMEOUT_SECONDS = 15.0


@dataclass
class CallOutcome:
    case: str  # "valid" | "missing_required" | "wrong_type"
    property_name: str | None
    outcome: str  # "ok" | "graceful_error" | "crash" | "timeout"
    detail: str = ""
    # Populated for --full-trace export (see mcp_fuzz.trace): the report's
    # own to_dict() intentionally still ignores these three fields for
    # every non-crash/timeout/valid_call_issue outcome, so the scored
    # --json report is unaffected by adding them here.
    arguments: dict = field(default_factory=dict)
    started_at: float = 0.0  # epoch seconds
    duration_ms: float = 0.0
    # Full (untruncated) character count of the response's joined text
    # content — unlike `detail` above (truncated to 300 chars for display),
    # this exists specifically to measure real response size, populated for
    # any outcome that got a real result back (not a crash/timeout, where
    # there's no response to measure).
    response_chars: int = 0
    # The full (untruncated) response text itself, same population rule as
    # response_chars above — exists specifically for the sequential check
    # to extract a real resource id from, where a 300-char truncation could
    # cut off the id field on a verbose create response.
    full_text: str = ""


@dataclass
class ToolResult:
    name: str
    tested: bool
    skip_reason: str | None = None
    outcomes: list[CallOutcome] = field(default_factory=list)
    # Populated only when concurrency > 0 in run_fuzz — see
    # _run_concurrent_valid_calls. Each outcome comes from an independently
    # launched connection (a separate subprocess of the same server
    # command), not the shared sequential connection above, so N of these
    # running at once is a real test of concurrent access to whatever
    # backend the server itself talks to (a shared file, database, lock),
    # not just "can one connection's event loop juggle two in-flight
    # requests."
    concurrent_outcomes: list[CallOutcome] = field(default_factory=list)


@dataclass
class SequenceStep:
    tool: str
    role: str  # "create" | "read" | "delete" | "read_after_delete"
    outcome: CallOutcome


@dataclass
class SequenceResult:
    resource: str
    create_tool: str
    read_tool: str | None
    delete_tool: str | None
    steps: list[SequenceStep] = field(default_factory=list)
    extracted_id: str | None = None
    stale_after_delete: bool = False
    note: str = ""


@dataclass
class FuzzReport:
    server_command: str
    tools: list[ToolResult] = field(default_factory=list)
    connect_error: str | None = None
    # Populated only when sequential=True in run_fuzz — see
    # _run_sequence_checks. Each entry is one detected create/read/delete
    # resource group, chained with a real id from the real create response
    # rather than independent synthetic calls like everything else here.
    sequence_results: list[SequenceResult] = field(default_factory=list)


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
        full_text = "; ".join(
            c.text for c in result.content if isinstance(c, types.TextContent)
        )
        return CallOutcome(case, property_name, outcome, full_text[:300], response_chars=len(full_text), full_text=full_text)

    full_text = "; ".join(
        c.text for c in result.content if isinstance(c, types.TextContent)
    ) if isinstance(result, types.CallToolResult) else ""
    return CallOutcome(case, property_name, "ok", response_chars=len(full_text), full_text=full_text)


def _merged_env(env: dict[str, str] | None) -> dict[str, str] | None:
    """`StdioServerParameters(env=None)` doesn't inherit the operator's shell —
    the SDK's own `stdio_client` deliberately falls back to a minimal safe
    allowlist (PATH, HOME, ...), never arbitrary app-specific vars, as a real
    security default against leaking secrets into a launched server. A
    caller that *does* pass `env` almost always means "also set this one API
    key", not "replace PATH/HOME entirely" — verified against a real
    failure: `brave/brave-search-mcp-server` refuses to start at all without
    `BRAVE_API_KEY`, and passing just that one var with no PATH would break
    `npx` before the target server ever runs. Merge onto the same safe
    baseline the SDK already uses when `env` is left unset, rather than
    replacing it; `env=None`/`{}` is passed through unchanged so the SDK's
    own default still applies exactly as before this existed."""
    return {**get_default_environment(), **env} if env else env


async def _run_concurrent_valid_calls(
    params: StdioServerParameters, tool_name: str, valid_args: dict, timeout: float, concurrency: int,
) -> list[CallOutcome]:
    """Launches `concurrency` independent connections (each its own
    subprocess of the target server command) and calls the same tool with
    the same valid arguments on all of them at once via asyncio.gather —
    a real test of concurrent access to whatever shared backend the server
    itself talks to (a shared file, database, lock), which N sequential
    calls on one connection can never exercise. Each connection is only
    ever touched by its own coroutine, so there's no shared mutable state
    between them on mcp-fuzz's own side to race on — any crash/timeout
    caught here is the target server's own concurrency behavior, not an
    artifact of how this function drives it."""
    async def _one_call() -> CallOutcome:
        conn = _ServerConnection(params)
        try:
            await conn.connect()
        except Exception as exc:
            return CallOutcome("valid", None, "crash", f"failed to connect: {type(exc).__name__}: {exc}")
        try:
            return await _call_with_outcome(conn, params, tool_name, "valid", None, valid_args, timeout)
        finally:
            await conn.close()

    return list(await asyncio.gather(*(_one_call() for _ in range(concurrency))))


async def _run_sequence_checks(
    conn: _ServerConnection,
    params: StdioServerParameters,
    tools: list[types.Tool],
    timeout: float,
) -> list[SequenceResult]:
    """For each detected create/read/delete resource group (see
    mcp_fuzz.sequence.group_resource_tools), creates a real resource, chains
    the real id it returns into the read/delete calls (instead of each
    tool's own independent synthetic arguments), and — when both a read and
    a delete tool exist — re-reads the same id after deletion to check for
    a stale read: the read tool still reporting success on a resource that
    was just removed. That specific check is the reason this exists; every
    other call above it is necessary setup, not the finding itself."""
    tools_by_name = {t.name: t for t in tools}
    groups = group_resource_tools([t.name for t in tools])
    results: list[SequenceResult] = []

    for group in groups:
        result = SequenceResult(
            resource=group.resource, create_tool=group.create_tool,
            read_tool=group.read_tool, delete_tool=group.delete_tool,
        )

        create_schema = _field(tools_by_name[group.create_tool], "input_schema", "inputSchema")
        create_args = generate_valid_arguments(create_schema)
        create_outcome = await _call_with_outcome(conn, params, group.create_tool, "valid", None, create_args, timeout)
        result.steps.append(SequenceStep(tool=group.create_tool, role="create", outcome=create_outcome))

        if create_outcome.outcome != "ok":
            result.note = f"create call did not succeed ({create_outcome.outcome}) — sequence stops here"
            results.append(result)
            continue

        extracted = extract_id(create_outcome.full_text, group.resource)
        if extracted is None:
            result.note = "create call succeeded but no id could be extracted from its response — sequence stops here"
            results.append(result)
            continue
        result.extracted_id = extracted

        read_schema = None
        read_id_prop = None
        pre_delete_read_ok = False
        if group.read_tool:
            read_schema = _field(tools_by_name[group.read_tool], "input_schema", "inputSchema")
            read_id_prop = find_id_property(read_schema, group.resource)
            if read_id_prop is None:
                result.note = f"could not determine which parameter on {group.read_tool} identifies the resource — skipping read step(s)"
            else:
                read_args = generate_valid_arguments(read_schema)
                read_args[read_id_prop] = extracted
                read_outcome = await _call_with_outcome(conn, params, group.read_tool, "valid", None, read_args, timeout)
                result.steps.append(SequenceStep(tool=group.read_tool, role="read", outcome=read_outcome))
                pre_delete_read_ok = read_outcome.outcome == "ok"

        if group.delete_tool:
            delete_schema = _field(tools_by_name[group.delete_tool], "input_schema", "inputSchema")
            delete_id_prop = find_id_property(delete_schema, group.resource)
            if delete_id_prop is None:
                note = f"could not determine which parameter on {group.delete_tool} identifies the resource — skipping delete step"
                result.note = f"{result.note}; {note}" if result.note else note
            else:
                delete_args = generate_valid_arguments(delete_schema)
                delete_args[delete_id_prop] = extracted
                delete_outcome = await _call_with_outcome(conn, params, group.delete_tool, "valid", None, delete_args, timeout)
                result.steps.append(SequenceStep(tool=group.delete_tool, role="delete", outcome=delete_outcome))

                if delete_outcome.outcome == "ok" and read_id_prop is not None:
                    read_args = generate_valid_arguments(read_schema)
                    read_args[read_id_prop] = extracted
                    post_delete_outcome = await _call_with_outcome(
                        conn, params, group.read_tool, "valid", None, read_args, timeout,
                    )
                    result.steps.append(SequenceStep(tool=group.read_tool, role="read_after_delete", outcome=post_delete_outcome))
                    if post_delete_outcome.outcome == "ok":
                        result.stale_after_delete = True
                        result.note = (
                            f"{group.read_tool} still returned success reading the id {group.delete_tool} "
                            f"just deleted{' (also succeeded before deletion, so this is a real change)' if pre_delete_read_ok else ''}"
                        )

        results.append(result)

    return results


async def run_fuzz(
    command: str,
    args: list[str] | None = None,
    env: dict[str, str] | None = None,
    cwd: str | None = None,
    include_destructive: bool = False,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
    concurrency: int = 0,
    sequential: bool = False,
) -> FuzzReport:
    merged_env = _merged_env(env)
    params = StdioServerParameters(command=command, args=args or [], env=merged_env, cwd=cwd)
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

        async def _timed_call(case: str, prop_name: str | None, call_args: dict) -> CallOutcome:
            # Deliberately wraps _call_with_outcome from the outside rather
            # than threading timing/arguments through its own return
            # statements: that function's crash/timeout/graceful_error
            # classification is carefully verified against real repos (see
            # its own comments), and duplicating call_args/timestamp capture
            # across every one of its return sites would risk a transcription
            # slip in logic that's already correct. A dataclass field set
            # after construction can't affect anything upstream that already
            # inspects `outcome`/`detail`.
            started = time.time()
            outcome = await _call_with_outcome(conn, params, tool.name, case, prop_name, call_args, timeout)
            outcome.arguments = call_args
            outcome.started_at = started
            outcome.duration_ms = (time.time() - started) * 1000
            return outcome

        valid_args = generate_valid_arguments(schema)
        result.outcomes.append(await _timed_call("valid", None, valid_args))

        for prop_name, args in missing_required_variants(schema):
            result.outcomes.append(await _timed_call("missing_required", prop_name, args))

        for prop_name, args in wrong_type_variants(schema):
            result.outcomes.append(await _timed_call("wrong_type", prop_name, args))

        if concurrency > 0:
            result.concurrent_outcomes = await _run_concurrent_valid_calls(
                params, tool.name, valid_args, timeout, concurrency,
            )

        report.tools.append(result)

    if sequential and include_destructive:
        # Requires include_destructive: a resource-lifecycle check by
        # definition creates and deletes a real resource, strictly more
        # destructive than testing a single write tool in isolation — never
        # run implicitly just because --sequential was passed.
        report.sequence_results = await _run_sequence_checks(
            conn, params, tools_result.tools, timeout,
        )

    await conn.close()
    return report
