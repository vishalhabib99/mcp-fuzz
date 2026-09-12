"""Unit tests for the latency check in report.py — fast, synthetic
ToolReport/CallOutcome construction rather than a real fixture-server call,
since the logic being tested (thresholds, median, exclusion of
crashed/timed-out calls) doesn't depend on the engine at all. See
test_engine.py's test_every_outcome_carries_real_timing for proof the real
duration comes from an actual wall-clock measurement, and
test_fixture_slow_tool_is_flagged_end_to_end below for proof it flows
through build_report correctly against a real running server."""

import sys
from pathlib import Path

import pytest

from mcp_fuzz.engine import CallOutcome, run_fuzz
from mcp_fuzz.report import LATENCY_ABSOLUTE_SLOW_MS, ToolReport, _compute_latency, build_report

FIXTURE_SERVER = str(Path(__file__).parent / "fixtures" / "fixture_server.py")


def _tool(name: str, duration_ms: float, tested: bool = True) -> ToolReport:
    tr = ToolReport(name=name, tested=tested, skip_reason=None)
    tr.valid_call_duration_ms = duration_ms
    return tr


def test_lone_slow_tool_flagged_by_absolute_threshold():
    tools = [_tool("slow_one", LATENCY_ABSOLUTE_SLOW_MS + 1)]
    summary = _compute_latency(tools, LATENCY_ABSOLUTE_SLOW_MS)
    assert summary.checked_count == 1
    assert [f.name for f in summary.slow_tools] == ["slow_one"]
    assert "absolute threshold" in summary.slow_tools[0].reasons[0]


def test_fast_tool_under_threshold_not_flagged():
    tools = [_tool("fast_one", 50.0)]
    summary = _compute_latency(tools, LATENCY_ABSOLUTE_SLOW_MS)
    assert summary.slow_tools == []
    assert summary.percent == 100.0


def test_relative_outlier_flagged_among_otherwise_fast_tools():
    # All four calls are well under the absolute threshold, but one is a
    # real 10x outlier relative to its own server's other tools — exactly
    # the "one unbounded upstream call among otherwise-fast tools" case
    # this check exists for.
    tools = [_tool("fast_a", 50.0), _tool("fast_b", 60.0), _tool("fast_c", 55.0), _tool("outlier", 600.0)]
    summary = _compute_latency(tools, LATENCY_ABSOLUTE_SLOW_MS)
    assert [f.name for f in summary.slow_tools] == ["outlier"]
    assert "median" in summary.slow_tools[0].reasons[0]


def test_no_relative_outlier_check_below_minimum_sample_size():
    # Only 2 tools — not enough to fairly call either one an "outlier",
    # even though one is 10x the other. Neither should be flagged.
    tools = [_tool("a", 50.0), _tool("b", 500.0)]
    summary = _compute_latency(tools, LATENCY_ABSOLUTE_SLOW_MS)
    assert summary.slow_tools == []


def test_crashed_or_timed_out_valid_call_excluded_from_latency():
    # A tool with no valid_call_duration_ms (crash/timeout contaminated
    # timing) is simply absent from the latency check entirely — it's
    # already surfaced by the crash-resilience score, not double-counted
    # here as if it were merely "slow".
    tools = [_tool("crashed", 50.0, tested=True)]
    tools[0].valid_call_duration_ms = None
    summary = _compute_latency(tools, LATENCY_ABSOLUTE_SLOW_MS)
    assert summary.checked_count == 0
    assert summary.percent is None
    assert summary.grade is None


def test_custom_slow_threshold_overrides_default():
    tools = [_tool("borderline", 200.0)]
    assert _compute_latency(tools, 5000.0).slow_tools == []
    assert _compute_latency(tools, 100.0).slow_tools != []


@pytest.fixture(scope="module")
def fixture_report():
    import asyncio

    return asyncio.run(run_fuzz(sys.executable, [FIXTURE_SERVER], timeout=5.0))


def test_fixture_slow_tool_is_flagged_end_to_end(fixture_report):
    # Proves the real engine's wall-clock duration_ms (not a synthetic
    # value) flows all the way through build_report into a real flag —
    # slow_but_fine sleeps 1.2s for real, well under the 5s test timeout
    # (so it's a graceful "ok", not a timeout) but a clear relative outlier
    # against this fixture's other near-instant tools.
    report = build_report(fixture_report)
    slow_names = {f.name for f in report.latency.slow_tools}
    assert "slow_but_fine" in slow_names
    assert "well_behaved" not in slow_names
