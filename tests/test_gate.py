"""Unit tests for LatencyGate's pure per-tool-history logic (deterministic,
no subprocess needed — mirrors test_report.py's split of pure heuristic
tests vs. end-to-end fixture tests), plus real end-to-end tests against the
actual fixture server for timed_call's wiring (crash/timeout classification
and the two absolute-threshold tools already in the fixture)."""

import asyncio
import sys
from contextlib import AsyncExitStack
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from mcp_fuzz.gate import LatencyGate

FIXTURE_SERVER = str(Path(__file__).parent / "fixtures" / "fixture_server.py")


# --- Pure LatencyGate.record() tests -----------------------------------

def test_lone_slow_call_flagged_by_absolute_threshold():
    gate = LatencyGate()
    result = gate.record("tool_a", duration_ms=6000.0, response_chars=10)
    assert result.flagged is True
    assert any("absolute threshold" in r for r in result.slow_reasons)


def test_fast_call_not_flagged():
    gate = LatencyGate()
    result = gate.record("tool_a", duration_ms=50.0, response_chars=10)
    assert result.flagged is False


def test_relative_outlier_flagged_against_the_same_tools_own_history():
    gate = LatencyGate(slow_threshold_ms=999999.0)  # isolate the relative signal
    for _ in range(3):
        gate.record("tool_a", duration_ms=100.0, response_chars=10)
    result = gate.record("tool_a", duration_ms=1000.0, response_chars=10)
    assert result.flagged is True
    assert any("own median" in r for r in result.slow_reasons)


def test_a_different_tools_history_does_not_affect_this_ones_outlier_check():
    # The whole point of tracking history per-tool, not per-session: tool_b
    # being reliably slow must never make tool_a's normal-for-tool_a call
    # look like an outlier, and vice versa.
    gate = LatencyGate(slow_threshold_ms=999999.0)
    for _ in range(5):
        gate.record("tool_b", duration_ms=5000.0, response_chars=10)
    result = gate.record("tool_a", duration_ms=120.0, response_chars=10)
    assert result.flagged is False


def test_no_relative_outlier_check_below_minimum_sample_size():
    gate = LatencyGate(slow_threshold_ms=999999.0, min_calls_for_latency_outlier=3)
    gate.record("tool_a", duration_ms=100.0, response_chars=10)
    result = gate.record("tool_a", duration_ms=1000.0, response_chars=10)  # only 1 prior call
    assert result.flagged is False


def test_lone_bloated_call_flagged_by_absolute_threshold():
    gate = LatencyGate()
    result = gate.record("tool_a", duration_ms=10.0, response_chars=50000)
    assert result.flagged is True
    assert any("absolute threshold" in r for r in result.bloated_reasons)


def test_relative_size_outlier_flagged_against_the_same_tools_own_history():
    gate = LatencyGate(bloat_threshold_chars=999999999)  # isolate the relative signal
    for _ in range(3):
        gate.record("tool_a", duration_ms=10.0, response_chars=100)
    result = gate.record("tool_a", duration_ms=10.0, response_chars=1000)
    assert result.flagged is True
    assert any("own median" in r for r in result.bloated_reasons)


def test_custom_thresholds_override_defaults():
    gate = LatencyGate(slow_threshold_ms=10.0, bloat_threshold_chars=5)
    result = gate.record("tool_a", duration_ms=20.0, response_chars=6)
    assert result.flagged is True
    assert any("10ms" in r for r in result.slow_reasons)
    assert any("5-char" in r for r in result.bloated_reasons)


# --- End-to-end timed_call() tests, real subprocess ---------------------

async def _call(tool_name: str, arguments: dict, gate: LatencyGate | None = None, timeout: float = 3.0):
    params = StdioServerParameters(command=sys.executable, args=[FIXTURE_SERVER])
    gate = gate or LatencyGate()
    async with AsyncExitStack() as stack:
        read, write = await stack.enter_async_context(stdio_client(params))
        session: ClientSession = await stack.enter_async_context(ClientSession(read, write))
        await session.initialize()
        return await gate.timed_call(session, tool_name, arguments, timeout=timeout)


def test_well_behaved_call_is_not_flagged():
    result = asyncio.run(_call("well_behaved", {"name": "x", "count": 1}))
    assert result.outcome == "ok"
    assert result.flagged is False


def test_slow_but_fine_tool_flagged_relative_to_a_lower_custom_threshold():
    # 1.2s is well under the 5s default, so use a tighter threshold to
    # verify timed_call's real measured duration actually reaches record().
    gate = LatencyGate(slow_threshold_ms=500.0)
    result = asyncio.run(_call("slow_but_fine", {"value": "x"}, gate=gate))
    assert result.outcome == "ok"
    assert result.flagged is True
    assert any("absolute threshold" in r for r in result.slow_reasons)


def test_bloated_but_fine_tool_flagged_relative_to_a_lower_custom_threshold():
    # "x" * 10000 = 10000 chars, under the 20000-char default — use a
    # tighter threshold to verify timed_call's real measured size actually
    # reaches record(), same reasoning as the slow_but_fine test above.
    gate = LatencyGate(bloat_threshold_chars=5000)
    result = asyncio.run(_call("bloated_but_fine", {"value": "x"}, gate=gate))
    assert result.outcome == "ok"
    assert result.flagged is True
    assert any("absolute threshold" in r for r in result.bloated_reasons)


def test_process_crash_is_classified_as_crash():
    result = asyncio.run(_call("kills_process", {"x": "anything"}))
    assert result.outcome == "crash"
    assert result.flagged is False


def test_hang_is_classified_as_timeout():
    result = asyncio.run(_call("hangs_forever", {"value": "x"}, timeout=1.0))
    assert result.outcome == "timeout"
    assert "1.0s" in result.detail
