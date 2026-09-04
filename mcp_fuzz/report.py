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


@dataclass
class ToolReport:
    name: str
    tested: bool
    skip_reason: str | None
    crashes: list[CallOutcome] = field(default_factory=list)
    timeouts: list[CallOutcome] = field(default_factory=list)
    valid_call_issue: CallOutcome | None = None
    bad_input_case_count: int = 0


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


def build_report(raw: FuzzReport) -> Report:
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
    lines.append("")

    for tool in report.tools:
        if not tool.tested:
            lines.append(f"  [skip] {tool.name} — {tool.skip_reason}")
            continue
        flags = []
        if tool.crashes:
            flags.append(f"{len(tool.crashes)} crash(es)")
        if tool.timeouts:
            flags.append(f"{len(tool.timeouts)} timeout(s)")
        if tool.valid_call_issue:
            flags.append(f"valid call: {tool.valid_call_issue.outcome}")
        marker = "FAIL" if (tool.crashes or tool.timeouts) else ("WARN" if tool.valid_call_issue else "ok")
        summary = f" — {'; '.join(flags)}" if flags else ""
        lines.append(f"  [{marker}] {tool.name}{summary}")
        for outcome in tool.crashes + tool.timeouts:
            lines.append(f"      {outcome.case} ({outcome.property_name}): {outcome.detail}")
        if tool.valid_call_issue:
            lines.append(
                f"      valid call — {tool.valid_call_issue.outcome}: {tool.valid_call_issue.detail} "
                "(may be a synthetic-input false positive, not a confirmed bug — see README)"
            )

    return "\n".join(lines)


def to_dict(report: Report) -> dict:
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
        "tools": [
            {
                "name": t.name,
                "tested": t.tested,
                "skip_reason": t.skip_reason,
                "crashes": [_outcome_dict(o) for o in t.crashes],
                "timeouts": [_outcome_dict(o) for o in t.timeouts],
                "valid_call_issue": _outcome_dict(t.valid_call_issue) if t.valid_call_issue else None,
                "bad_input_case_count": t.bad_input_case_count,
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
