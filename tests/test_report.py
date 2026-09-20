"""Unit tests for the latency check in report.py — fast, synthetic
ToolReport/CallOutcome construction rather than a real fixture-server call,
since the logic being tested (thresholds, median, exclusion of
crashed/timed-out calls) doesn't depend on the engine at all. See
test_engine.py's test_every_outcome_carries_real_timing for proof the real
duration comes from an actual wall-clock measurement, and
test_fixture_slow_tool_is_flagged_end_to_end below for proof it flows
through build_report correctly against a real running server."""

import os
import sys
import tempfile
from pathlib import Path

import pytest

from mcp_fuzz.engine import CallOutcome, run_fuzz
from mcp_fuzz.report import (
    CHARS_PER_TOKEN_ESTIMATE,
    LATENCY_ABSOLUTE_SLOW_MS,
    RESPONSE_SIZE_ABSOLUTE_CHARS,
    ToolReport,
    _compute_concurrency,
    _compute_latency,
    _compute_response_size,
    _compute_token_cost,
    build_report,
)

FIXTURE_SERVER = str(Path(__file__).parent / "fixtures" / "fixture_server.py")


def _concurrency_tool(name: str, outcomes: list[str], tested: bool = True) -> ToolReport:
    tr = ToolReport(name=name, tested=tested, skip_reason=None)
    tr.concurrent_outcomes = [CallOutcome("valid", None, o) for o in outcomes]
    return tr


def _tool(name: str, duration_ms: float, tested: bool = True) -> ToolReport:
    tr = ToolReport(name=name, tested=tested, skip_reason=None)
    tr.valid_call_duration_ms = duration_ms
    return tr


def _sized_tool(name: str, response_chars: int, tested: bool = True) -> ToolReport:
    tr = ToolReport(name=name, tested=tested, skip_reason=None)
    tr.valid_call_response_chars = response_chars
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


def test_lone_bloated_tool_flagged_by_absolute_threshold():
    tools = [_sized_tool("bloated_one", RESPONSE_SIZE_ABSOLUTE_CHARS + 1)]
    summary = _compute_response_size(tools, RESPONSE_SIZE_ABSOLUTE_CHARS)
    assert summary.checked_count == 1
    assert [f.name for f in summary.bloated_tools] == ["bloated_one"]
    assert "absolute threshold" in summary.bloated_tools[0].reasons[0]


def test_small_response_under_threshold_not_flagged():
    tools = [_sized_tool("small_one", 200)]
    summary = _compute_response_size(tools, RESPONSE_SIZE_ABSOLUTE_CHARS)
    assert summary.bloated_tools == []
    assert summary.percent == 100.0


def test_relative_size_outlier_flagged_among_otherwise_small_tools():
    tools = [
        _sized_tool("small_a", 200), _sized_tool("small_b", 250),
        _sized_tool("small_c", 220), _sized_tool("outlier", 5000),
    ]
    summary = _compute_response_size(tools, RESPONSE_SIZE_ABSOLUTE_CHARS)
    assert [f.name for f in summary.bloated_tools] == ["outlier"]
    assert "median" in summary.bloated_tools[0].reasons[0]


def test_no_relative_size_outlier_check_below_minimum_sample_size():
    tools = [_sized_tool("a", 200), _sized_tool("b", 3000)]
    summary = _compute_response_size(tools, RESPONSE_SIZE_ABSOLUTE_CHARS)
    assert summary.bloated_tools == []


def test_crashed_or_timed_out_valid_call_excluded_from_response_size():
    tools = [_sized_tool("crashed", 200, tested=True)]
    tools[0].valid_call_response_chars = None
    summary = _compute_response_size(tools, RESPONSE_SIZE_ABSOLUTE_CHARS)
    assert summary.checked_count == 0
    assert summary.percent is None
    assert summary.grade is None


def test_custom_bloat_threshold_overrides_default():
    tools = [_sized_tool("borderline", 1000)]
    assert _compute_response_size(tools, 5000).bloated_tools == []
    assert _compute_response_size(tools, 500).bloated_tools != []


def test_token_cost_sums_across_tools_using_char_estimate():
    tools = [_sized_tool("a", 400), _sized_tool("b", 800)]
    summary = _compute_token_cost(tools)
    assert summary.checked_count == 2
    assert summary.total_tokens_estimated == (400 + 800) // CHARS_PER_TOKEN_ESTIMATE
    assert summary.avg_tokens_per_call == summary.total_tokens_estimated / 2


def test_token_cost_none_when_no_sized_tools():
    tools = [_sized_tool("crashed", 200)]
    tools[0].valid_call_response_chars = None
    summary = _compute_token_cost(tools)
    assert summary.checked_count == 0
    assert summary.total_tokens_estimated is None
    assert summary.avg_tokens_per_call is None


def test_token_cost_excludes_crashed_or_timed_out_calls_from_total():
    # Same exclusion as response_size: a crashed/timed-out valid call has no
    # real response to count the size of, so it shouldn't silently pull the
    # session total down (or up) as if it cost zero tokens.
    tools = [_sized_tool("ok", 400), _sized_tool("crashed", 200)]
    tools[1].valid_call_response_chars = None
    summary = _compute_token_cost(tools)
    assert summary.checked_count == 1
    assert summary.total_tokens_estimated == 400 // CHARS_PER_TOKEN_ESTIMATE


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


def test_fixture_bloated_tool_is_flagged_end_to_end(fixture_report):
    # Proves the real engine's actual response length (not a synthetic
    # value) flows all the way through build_report into a real flag —
    # bloated_but_fine returns its input repeated 10000x, a clear outlier
    # against this fixture's other tiny-response tools.
    report = build_report(fixture_report)
    bloated_names = {f.name for f in report.response_size.bloated_tools}
    assert "bloated_but_fine" in bloated_names
    assert "well_behaved" not in bloated_names


def test_fixture_token_cost_reflects_real_response_sizes_end_to_end(fixture_report):
    # Proves the total is built from the engine's real response_chars, not
    # a fixed/synthetic value — bloated_but_fine's real 10000x-repeated
    # response should dominate the fixture's total token estimate.
    report = build_report(fixture_report)
    tc = report.token_cost
    assert tc.checked_count > 0
    assert tc.total_tokens_estimated is not None
    bloated = next(t for t in report.tools if t.name == "bloated_but_fine")
    assert tc.total_tokens_estimated > (bloated.valid_call_response_chars // CHARS_PER_TOKEN_ESTIMATE) * 0.5


def test_concurrency_not_computed_when_no_tool_was_concurrency_tested():
    tools = [_tool("a", 50.0)]  # valid_call_duration_ms set, concurrent_outcomes empty
    summary = _compute_concurrency(tools)
    assert summary.checked_count == 0
    assert summary.percent is None


def test_tool_that_crashes_under_concurrency_is_flagged():
    tools = [_concurrency_tool("racy", ["ok", "crash", "ok", "crash"])]
    summary = _compute_concurrency(tools)
    assert summary.concurrency == 4
    assert [f.name for f in summary.flagged_tools] == ["racy"]
    assert summary.flagged_tools[0].crashes == 2
    assert summary.percent == 0.0


def test_tool_clean_under_concurrency_is_not_flagged():
    tools = [_concurrency_tool("safe", ["ok", "ok", "ok"])]
    summary = _compute_concurrency(tools)
    assert summary.flagged_tools == []
    assert summary.percent == 100.0


def test_concurrency_timeout_is_flagged_same_as_crash():
    tools = [_concurrency_tool("hangs_sometimes", ["ok", "timeout", "ok"])]
    summary = _compute_concurrency(tools)
    assert summary.flagged_tools[0].timeouts == 1


@pytest.fixture(scope="module")
def concurrency_report():
    import asyncio

    lock_path = os.path.join(tempfile.gettempdir(), "mcp_fuzz_fixture_concurrency_lock")
    if os.path.exists(lock_path):
        os.remove(lock_path)  # stale lock from a previous crashed run
    return asyncio.run(run_fuzz(sys.executable, [FIXTURE_SERVER], timeout=5.0, concurrency=5))


def test_fixture_racy_tool_is_flagged_under_real_concurrency(concurrency_report):
    # Proves the real engine actually launches independent concurrent
    # connections (not a synthetic outcome list) and that a genuine,
    # reproducible race — a naive create-exclusive lock file with no
    # retry/queue handling — gets caught: with 5 concurrent connections all
    # calling not_concurrency_safe at once, at least one should collide on
    # the shared lock file and raise FileExistsError.
    report = build_report(concurrency_report)
    flagged_names = {f.name for f in report.concurrency.flagged_tools}
    assert "not_concurrency_safe" in flagged_names
    assert "well_behaved" not in flagged_names
