"""Turns a raw FuzzReport into a scored, human- or JSON-readable report.

The score is deliberately narrow: it's a *crash-resilience* score — the
fraction of deliberately-bad-input calls (a missing required field, a
wrong-typed field) that the server handled with a structured error instead
of crashing or hanging. It does NOT grade whether a tool's "valid" call
produced a *correct* result, since a synthetic, schema-only-derived value
(e.g. a placeholder string for a field that's really supposed to be a real
arXiv ID or a reachable URL) commonly isn't realistic enough for that to be
a fair judgment — see `mcp_fuzz.engine`'s "valid_call_errored" outcome,
which is reported separately as "worth investigating", not folded into the
score, precisely because it can be a false positive from unrealistic
synthetic data rather than a real tool bug.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from mcp_fuzz.engine import CallOutcome, FuzzReport, ToolResult

BAD_INPUT_CASES = {"missing_required", "wrong_type"}

# A tool's "valid" call is the one real, representative round-trip for
# latency purposes — bad-input calls are typically rejected before the
# server does any real work, so timing them would understate a slow tool's
# real cost. Two independent signals, either one enough to flag a tool:
# an absolute ceiling (a lone slow tool is still slow even with nothing to
# compare it to) and a relative-outlier check against this server's own
# other tools (a fair comparison only once there's enough of a sample to
# call something an outlier at all).
LATENCY_ABSOLUTE_SLOW_MS = 5000.0
LATENCY_OUTLIER_MULTIPLIER = 3.0
MIN_TOOLS_FOR_RELATIVE_OUTLIER = 3


@dataclass
class ToolReport:
    name: str
    tested: bool
    skip_reason: str | None
    crashes: list[CallOutcome] = field(default_factory=list)
    timeouts: list[CallOutcome] = field(default_factory=list)
    valid_call_issue: CallOutcome | None = None
    bad_input_case_count: int = 0
    # None when the tool wasn't tested, or its valid call crashed/timed out —
    # that duration is contaminated by reconnect/timeout overhead, not a real
    # measurement of the server's own processing time, and the crash/timeout
    # is already surfaced by the crash-resilience score above.
    valid_call_duration_ms: float | None = None


@dataclass
class LatencyFlag:
    name: str
    duration_ms: float
    reasons: list[str]


@dataclass
class LatencySummary:
    checked_count: int
    median_ms: float | None
    slow_tools: list[LatencyFlag]
    percent: float | None
    grade: str | None


@dataclass
class Report:
    server_command: str
    connect_error: str | None
    tools: list[ToolReport]
    tested_count: int
    skipped_count: int
    total_bad_input_cases: int
    crash_count: int
    timeout_count: int
    crash_resilience_percent: float | None
    grade: str | None
    latency: LatencySummary


def _grade_for_percent(pct: float) -> str:
    if pct >= 97:
        return "A"
    if pct >= 90:
        return "B"
    if pct >= 75:
        return "C"
    if pct >= 50:
        return "D"
    return "F"


def build_report(raw: FuzzReport, slow_threshold_ms: float = LATENCY_ABSOLUTE_SLOW_MS) -> Report:
    tool_reports: list[ToolReport] = []
    total_bad_input = 0
    total_crashes = 0
    total_timeouts = 0
    tested_count = 0
    skipped_count = 0

    for tool in raw.tools:
        if not tool.tested:
            skipped_count += 1
            tool_reports.append(ToolReport(
                name=tool.name, tested=False, skip_reason=tool.skip_reason,
            ))
            continue

        tested_count += 1
        tr = ToolReport(name=tool.name, tested=True, skip_reason=None)
        for outcome in tool.outcomes:
            if outcome.case == "valid":
                if outcome.outcome in ("crash", "timeout", "valid_call_errored"):
                    tr.valid_call_issue = outcome
                if outcome.outcome not in ("crash", "timeout"):
                    tr.valid_call_duration_ms = outcome.duration_ms
                continue
            if outcome.case not in BAD_INPUT_CASES:
                continue
            tr.bad_input_case_count += 1
            total_bad_input += 1
            if outcome.outcome == "crash":
                tr.crashes.append(outcome)
                total_crashes += 1
            elif outcome.outcome == "timeout":
                tr.timeouts.append(outcome)
                total_timeouts += 1
        tool_reports.append(tr)

    if total_bad_input > 0:
        percent = 100.0 * (1 - (total_crashes + total_timeouts) / total_bad_input)
        grade = _grade_for_percent(percent)
    else:
        percent = None
        grade = None

    return Report(
        server_command=raw.server_command,
        connect_error=raw.connect_error,
        tools=tool_reports,
        tested_count=tested_count,
        skipped_count=skipped_count,
        total_bad_input_cases=total_bad_input,
        crash_count=total_crashes,
        timeout_count=total_timeouts,
        crash_resilience_percent=percent,
        grade=grade,
        latency=_compute_latency(tool_reports, slow_threshold_ms),
    )


def _median(values: list[float]) -> float:
    values = sorted(values)
    mid = len(values) // 2
    if len(values) % 2 == 1:
        return values[mid]
    return (values[mid - 1] + values[mid]) / 2


def _compute_latency(tool_reports: list[ToolReport], slow_threshold_ms: float) -> LatencySummary:
    timed = [(t.name, t.valid_call_duration_ms) for t in tool_reports if t.valid_call_duration_ms is not None]
    if not timed:
        return LatencySummary(checked_count=0, median_ms=None, slow_tools=[], percent=None, grade=None)

    median = _median([d for _, d in timed])
    enough_for_relative = len(timed) >= MIN_TOOLS_FOR_RELATIVE_OUTLIER and median > 0
    slow_tools: list[LatencyFlag] = []
    for name, duration in timed:
        reasons = []
        if duration > slow_threshold_ms:
            reasons.append(f"{duration:.0f}ms, over the {slow_threshold_ms:.0f}ms absolute threshold")
        if enough_for_relative and duration > LATENCY_OUTLIER_MULTIPLIER * median:
            reasons.append(f"{duration / median:.1f}x this server's median ({median:.0f}ms)")
        if reasons:
            slow_tools.append(LatencyFlag(name=name, duration_ms=duration, reasons=reasons))

    percent = 100.0 * (len(timed) - len(slow_tools)) / len(timed)
    grade = _grade_for_percent(percent)
    return LatencySummary(
        checked_count=len(timed), median_ms=median, slow_tools=slow_tools, percent=percent, grade=grade,
    )


def render_text(report: Report) -> str:
    lines: list[str] = []
    if report.connect_error:
        lines.append(f"Failed to connect: {report.connect_error}")
        return "\n".join(lines)

    lines.append(f"mcp-fuzz: {report.server_command}")
    lines.append("")
    if report.crash_resilience_percent is not None:
        lines.append(
            f"Crash resilience: {report.crash_resilience_percent:.0f}% ({report.grade}) "
            f"— {report.crash_count} crash(es), {report.timeout_count} timeout(s) "
            f"across {report.total_bad_input_cases} bad-input calls"
        )
    else:
        lines.append("Crash resilience: n/a (no testable tools had any parameters to fuzz)")
    lines.append(f"Tested {report.tested_count} tool(s), skipped {report.skipped_count} (not read-only)")

    lat = report.latency
    if lat.percent is not None:
        lines.append(
            f"Latency: {lat.percent:.0f}% ({lat.grade}) — {len(lat.slow_tools)} tool(s) flagged slow "
            f"out of {lat.checked_count} checked (server median {lat.median_ms:.0f}ms; "
            "single real call per tool, not a load test — see README)"
        )
    else:
        lines.append("Latency: n/a (no tool completed a timed valid call)")
    lines.append("")

    slow_by_name = {f.name: f for f in report.latency.slow_tools}

    for tool in report.tools:
        if not tool.tested:
            lines.append(f"  [skip] {tool.name} — {tool.skip_reason}")
            continue
        slow_flag = slow_by_name.get(tool.name)
        flags = []
        if tool.crashes:
            flags.append(f"{len(tool.crashes)} crash(es)")
        if tool.timeouts:
            flags.append(f"{len(tool.timeouts)} timeout(s)")
        if tool.valid_call_issue:
            flags.append(f"valid call: {tool.valid_call_issue.outcome}")
        if slow_flag:
            flags.append("slow")
        marker = "FAIL" if (tool.crashes or tool.timeouts) else ("WARN" if (tool.valid_call_issue or slow_flag) else "ok")
        summary = f" — {'; '.join(flags)}" if flags else ""
        lines.append(f"  [{marker}] {tool.name}{summary}")
        for outcome in tool.crashes + tool.timeouts:
            lines.append(f"      {outcome.case} ({outcome.property_name}): {outcome.detail}")
        if slow_flag:
            lines.append(f"      slow — {'; '.join(slow_flag.reasons)}")
        if tool.valid_call_issue:
            lines.append(
                f"      valid call — {tool.valid_call_issue.outcome}: {tool.valid_call_issue.detail} "
                "(may be a synthetic-input false positive, not a confirmed bug — see README)"
            )

    return "\n".join(lines)


def to_dict(report: Report) -> dict:
    slow_by_name = {f.name: f for f in report.latency.slow_tools}
    return {
        "server_command": report.server_command,
        "connect_error": report.connect_error,
        "tested_count": report.tested_count,
        "skipped_count": report.skipped_count,
        "total_bad_input_cases": report.total_bad_input_cases,
        "crash_count": report.crash_count,
        "timeout_count": report.timeout_count,
        "crash_resilience_percent": report.crash_resilience_percent,
        "grade": report.grade,
        "latency": {
            "checked_count": report.latency.checked_count,
            "median_ms": report.latency.median_ms,
            "percent": report.latency.percent,
            "grade": report.latency.grade,
            "slow_tools": [
                {"name": f.name, "duration_ms": f.duration_ms, "reasons": f.reasons}
                for f in report.latency.slow_tools
            ],
        },
        "tools": [
            {
                "name": t.name,
                "tested": t.tested,
                "skip_reason": t.skip_reason,
                "crashes": [_outcome_dict(o) for o in t.crashes],
                "timeouts": [_outcome_dict(o) for o in t.timeouts],
                "valid_call_issue": _outcome_dict(t.valid_call_issue) if t.valid_call_issue else None,
                "bad_input_case_count": t.bad_input_case_count,
                "valid_call_duration_ms": t.valid_call_duration_ms,
                "slow": slow_by_name.get(t.name).reasons if t.name in slow_by_name else None,
            }
            for t in report.tools
        ],
    }


def _outcome_dict(outcome: CallOutcome) -> dict:
    return {
        "case": outcome.case,
        "property": outcome.property_name,
        "outcome": outcome.outcome,
        "detail": outcome.detail,
    }
