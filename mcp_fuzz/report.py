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

from mcp_fuzz.engine import CallOutcome, FuzzReport, SequenceResult, ToolResult

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

# Same two-signal design as latency, applied to response size instead of
# response time: a tool that dumps an unusually large payload burns an
# agent's context window for no reason an agent can see coming from the
# tool's own description. 20000 chars (~5000 tokens on the common ~4
# chars/token rule of thumb for English text — not a real tokenizer, just a
# cheap estimate stated as such wherever it's shown) is deliberately
# generous: this flags genuinely bloated responses, not merely verbose ones.
RESPONSE_SIZE_ABSOLUTE_CHARS = 20000
RESPONSE_SIZE_OUTLIER_MULTIPLIER = 3.0
MIN_TOOLS_FOR_RESPONSE_SIZE_OUTLIER = 3


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
    # None for the same reasons as above — a crashed/timed-out call has no
    # real response to measure the size of.
    valid_call_response_chars: int | None = None
    # Only populated when --concurrency was used — see engine.py's
    # _run_concurrent_valid_calls. Each entry is one independent
    # connection's outcome from calling this tool at the same time as the
    # others; empty (not None) when concurrency testing wasn't requested,
    # so "tested but concurrency off" and "not tested at all" both read
    # naturally as "nothing to report" without needing a tri-state check.
    concurrent_outcomes: list[CallOutcome] = field(default_factory=list)


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
class ResponseSizeFlag:
    name: str
    response_chars: int
    reasons: list[str]


@dataclass
class ResponseSizeSummary:
    checked_count: int
    median_chars: float | None
    bloated_tools: list[ResponseSizeFlag]
    percent: float | None
    grade: str | None


@dataclass
class ConcurrencyFlag:
    name: str
    crashes: int
    timeouts: int
    errors: int
    concurrency: int
    reasons: list[str]


@dataclass
class ConcurrencySummary:
    concurrency: int  # 0 when not requested
    checked_count: int
    flagged_tools: list[ConcurrencyFlag]
    percent: float | None
    grade: str | None


@dataclass
class SequenceSummary:
    # Not scored with a percent/grade like the checks above — there's no
    # natural denominator (a server with zero detected create/read/delete
    # groups isn't "failing", it just has nothing this check can exercise).
    # Binary and explicit instead: how many groups were found, how many
    # produced a real stale-read finding.
    groups_detected: int
    stale_after_delete_count: int
    groups: list[SequenceResult]


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
    response_size: ResponseSizeSummary
    concurrency: ConcurrencySummary
    sequence: SequenceSummary


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


def build_report(
    raw: FuzzReport,
    slow_threshold_ms: float = LATENCY_ABSOLUTE_SLOW_MS,
    bloat_threshold_chars: int = RESPONSE_SIZE_ABSOLUTE_CHARS,
) -> Report:
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
        tr = ToolReport(name=tool.name, tested=True, skip_reason=None, concurrent_outcomes=tool.concurrent_outcomes)
        for outcome in tool.outcomes:
            if outcome.case == "valid":
                if outcome.outcome in ("crash", "timeout", "valid_call_errored"):
                    tr.valid_call_issue = outcome
                if outcome.outcome not in ("crash", "timeout"):
                    tr.valid_call_duration_ms = outcome.duration_ms
                    tr.valid_call_response_chars = outcome.response_chars
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
        response_size=_compute_response_size(tool_reports, bloat_threshold_chars),
        concurrency=_compute_concurrency(tool_reports),
        sequence=_compute_sequence(raw.sequence_results),
    )


def _compute_sequence(groups: list[SequenceResult]) -> SequenceSummary:
    stale_count = sum(1 for g in groups if g.stale_after_delete)
    return SequenceSummary(groups_detected=len(groups), stale_after_delete_count=stale_count, groups=groups)


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


def _compute_response_size(tool_reports: list[ToolReport], bloat_threshold_chars: int) -> ResponseSizeSummary:
    sized = [(t.name, t.valid_call_response_chars) for t in tool_reports if t.valid_call_response_chars is not None]
    if not sized:
        return ResponseSizeSummary(checked_count=0, median_chars=None, bloated_tools=[], percent=None, grade=None)

    median = _median([c for _, c in sized])
    enough_for_relative = len(sized) >= MIN_TOOLS_FOR_RESPONSE_SIZE_OUTLIER and median > 0
    bloated_tools: list[ResponseSizeFlag] = []
    for name, chars in sized:
        reasons = []
        if chars > bloat_threshold_chars:
            reasons.append(f"{chars:,} chars (~{chars // 4:,} est. tokens), over the {bloat_threshold_chars:,}-char absolute threshold")
        if enough_for_relative and chars > RESPONSE_SIZE_OUTLIER_MULTIPLIER * median:
            reasons.append(f"{chars / median:.1f}x this server's median ({median:.0f} chars)")
        if reasons:
            bloated_tools.append(ResponseSizeFlag(name=name, response_chars=chars, reasons=reasons))

    percent = 100.0 * (len(sized) - len(bloated_tools)) / len(sized)
    grade = _grade_for_percent(percent)
    return ResponseSizeSummary(
        checked_count=len(sized), median_chars=median, bloated_tools=bloated_tools, percent=percent, grade=grade,
    )


def _compute_concurrency(tool_reports: list[ToolReport]) -> ConcurrencySummary:
    checked = [t for t in tool_reports if t.concurrent_outcomes]
    if not checked:
        return ConcurrencySummary(concurrency=0, checked_count=0, flagged_tools=[], percent=None, grade=None)

    concurrency = len(checked[0].concurrent_outcomes)
    flagged: list[ConcurrencyFlag] = []
    for t in checked:
        n = len(t.concurrent_outcomes)
        crashes = sum(1 for o in t.concurrent_outcomes if o.outcome == "crash")
        timeouts = sum(1 for o in t.concurrent_outcomes if o.outcome == "timeout")
        # Every concurrent call uses the identical "valid" arguments, so
        # unlike the sequential valid-call check elsewhere (where an error
        # is reported leniently as "maybe a synthetic-input false
        # positive"), a valid_call_errored outcome here can't be explained
        # by unrealistic input. Frameworks like FastMCP commonly catch an
        # application-level exception and return it as a normal
        # isError:true response rather than a raw crash (verified directly:
        # the fixture's not_concurrency_safe raising FileExistsError under
        # real concurrent load surfaces exactly this way, not as "crash")
        # — treating it as anything less than a real concurrency finding
        # would silently miss most real bugs this check exists to catch.
        errors = sum(1 for o in t.concurrent_outcomes if o.outcome == "valid_call_errored")
        total_failed = crashes + timeouts + errors
        if total_failed == 0:
            continue
        # A tool whose own sequential valid call already failed (already
        # surfaced by valid_call_issue / the crash-resilience score) failing
        # the exact same way on every one of N concurrent calls isn't new
        # information — it's just broken, with or without concurrency.
        # Only worth a *concurrency* finding when either some concurrent
        # calls succeeded and others didn't (proves it's load-dependent) or
        # the tool works fine alone but breaks under concurrent load (a
        # real deadlock/starvation signature, arguably the more serious of
        # the two).
        if t.valid_call_issue is not None and total_failed == n:
            continue

        reasons = []
        if crashes:
            reasons.append(f"{crashes}/{n} concurrent calls crashed")
        if timeouts:
            reasons.append(f"{timeouts}/{n} concurrent calls timed out")
        if errors:
            if total_failed < n:
                reasons.append(f"{errors}/{n} concurrent calls errored on input other concurrent calls succeeded with")
            else:
                reasons.append(f"{errors}/{n} concurrent calls errored even though this tool's own sequential call succeeds — possible deadlock/starvation under load")
        flagged.append(ConcurrencyFlag(
            name=t.name, crashes=crashes, timeouts=timeouts, errors=errors,
            concurrency=concurrency, reasons=reasons,
        ))

    percent = 100.0 * (len(checked) - len(flagged)) / len(checked)
    grade = _grade_for_percent(percent)
    return ConcurrencySummary(
        concurrency=concurrency, checked_count=len(checked), flagged_tools=flagged, percent=percent, grade=grade,
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

    rsz = report.response_size
    if rsz.percent is not None:
        lines.append(
            f"Response size: {rsz.percent:.0f}% ({rsz.grade}) — {len(rsz.bloated_tools)} tool(s) flagged bloated "
            f"out of {rsz.checked_count} checked (server median {rsz.median_chars:.0f} chars; "
            "single real call per tool, not representative of every possible input — see README)"
        )
    else:
        lines.append("Response size: n/a (no tool completed a sized valid call)")

    conc = report.concurrency
    if conc.percent is not None:
        lines.append(
            f"Concurrency ({conc.concurrency}x): {conc.percent:.0f}% ({conc.grade}) — "
            f"{len(conc.flagged_tools)} tool(s) crashed or timed out under concurrent load "
            f"out of {conc.checked_count} checked"
        )

    seq = report.sequence
    if seq.groups_detected > 0:
        lines.append(
            f"Resource lifecycle: {seq.groups_detected} create/read/delete group(s) detected, "
            f"{seq.stale_after_delete_count} stale-after-delete finding(s)"
        )
    lines.append("")

    slow_by_name = {f.name: f for f in report.latency.slow_tools}
    bloated_by_name = {f.name: f for f in report.response_size.bloated_tools}
    concurrency_by_name = {f.name: f for f in report.concurrency.flagged_tools}

    for tool in report.tools:
        if not tool.tested:
            lines.append(f"  [skip] {tool.name} — {tool.skip_reason}")
            continue
        slow_flag = slow_by_name.get(tool.name)
        bloat_flag = bloated_by_name.get(tool.name)
        concurrency_flag = concurrency_by_name.get(tool.name)
        flags = []
        if tool.crashes:
            flags.append(f"{len(tool.crashes)} crash(es)")
        if tool.timeouts:
            flags.append(f"{len(tool.timeouts)} timeout(s)")
        if tool.valid_call_issue:
            flags.append(f"valid call: {tool.valid_call_issue.outcome}")
        if slow_flag:
            flags.append("slow")
        if bloat_flag:
            flags.append("bloated")
        if concurrency_flag:
            flags.append("unsafe under concurrency")
        marker = "FAIL" if (tool.crashes or tool.timeouts or concurrency_flag) else ("WARN" if (tool.valid_call_issue or slow_flag or bloat_flag) else "ok")
        summary = f" — {'; '.join(flags)}" if flags else ""
        lines.append(f"  [{marker}] {tool.name}{summary}")
        for outcome in tool.crashes + tool.timeouts:
            lines.append(f"      {outcome.case} ({outcome.property_name}): {outcome.detail}")
        if slow_flag:
            lines.append(f"      slow — {'; '.join(slow_flag.reasons)}")
        if bloat_flag:
            lines.append(f"      bloated — {'; '.join(bloat_flag.reasons)}")
        if concurrency_flag:
            lines.append(f"      unsafe under concurrency — {'; '.join(concurrency_flag.reasons)}")
        if tool.valid_call_issue:
            lines.append(
                f"      valid call — {tool.valid_call_issue.outcome}: {tool.valid_call_issue.detail} "
                "(may be a synthetic-input false positive, not a confirmed bug — see README)"
            )

    if seq.groups_detected > 0:
        lines.append("")
        lines.append("Resource lifecycle (real id chained from create into read/delete):")
        for g in seq.groups:
            marker = "FAIL" if g.stale_after_delete else "ok"
            chain = " -> ".join(s.tool for s in g.steps)
            lines.append(f"  [{marker}] {g.resource}: {chain}")
            if g.note:
                lines.append(f"      {g.note}")

    return "\n".join(lines)


def to_dict(report: Report) -> dict:
    slow_by_name = {f.name: f for f in report.latency.slow_tools}
    bloated_by_name = {f.name: f for f in report.response_size.bloated_tools}
    concurrency_by_name = {f.name: f for f in report.concurrency.flagged_tools}
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
        "response_size": {
            "checked_count": report.response_size.checked_count,
            "median_chars": report.response_size.median_chars,
            "percent": report.response_size.percent,
            "grade": report.response_size.grade,
            "bloated_tools": [
                {"name": f.name, "response_chars": f.response_chars, "reasons": f.reasons}
                for f in report.response_size.bloated_tools
            ],
        },
        "concurrency": {
            "concurrency": report.concurrency.concurrency,
            "checked_count": report.concurrency.checked_count,
            "percent": report.concurrency.percent,
            "grade": report.concurrency.grade,
            "flagged_tools": [
                {"name": f.name, "crashes": f.crashes, "timeouts": f.timeouts, "errors": f.errors, "reasons": f.reasons}
                for f in report.concurrency.flagged_tools
            ],
        },
        "sequence": {
            "groups_detected": report.sequence.groups_detected,
            "stale_after_delete_count": report.sequence.stale_after_delete_count,
            "groups": [
                {
                    "resource": g.resource,
                    "create_tool": g.create_tool,
                    "read_tool": g.read_tool,
                    "delete_tool": g.delete_tool,
                    "extracted_id": g.extracted_id,
                    "stale_after_delete": g.stale_after_delete,
                    "note": g.note,
                    "steps": [
                        {"tool": s.tool, "role": s.role, "outcome": _outcome_dict(s.outcome)}
                        for s in g.steps
                    ],
                }
                for g in report.sequence.groups
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
                "valid_call_response_chars": t.valid_call_response_chars,
                "slow": slow_by_name.get(t.name).reasons if t.name in slow_by_name else None,
                "bloated": bloated_by_name.get(t.name).reasons if t.name in bloated_by_name else None,
                "concurrency_unsafe": concurrency_by_name.get(t.name).reasons if t.name in concurrency_by_name else None,
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
