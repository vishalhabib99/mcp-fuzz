"""Unit tests for the --full-trace JSONL writer — these build a FuzzReport
by hand rather than launching the real fixture server (test_engine.py
already covers that end-to-end); this file is only about the writer's own
output shape."""

import json

from mcp_fuzz.engine import CallOutcome, FuzzReport, ToolResult
from mcp_fuzz.trace import write_jsonl_trace


def _read_lines(path):
    return [json.loads(line) for line in open(path)]


def test_writes_one_meta_line_first(tmp_path):
    report = FuzzReport(server_command="python server.py")
    out = tmp_path / "trace.jsonl"
    write_jsonl_trace(report, str(out))

    lines = _read_lines(out)
    assert lines[0]["event"] == "session.meta"
    assert lines[0]["serverCommand"] == "python server.py"


def test_skipped_tool_writes_a_skip_line_and_no_call_lines(tmp_path):
    report = FuzzReport(server_command="x", tools=[
        ToolResult(name="dangerous_tool", tested=False, skip_reason="not annotated readOnlyHint=true"),
    ])
    out = tmp_path / "trace.jsonl"
    write_jsonl_trace(report, str(out))

    lines = _read_lines(out)[1:]  # skip the meta line
    assert len(lines) == 1
    assert lines[0] == {
        "event": "tool.skipped",
        "tool": "dangerous_tool",
        "skipReason": "not annotated readOnlyHint=true",
    }


def test_tested_tool_writes_one_call_line_per_outcome_with_full_fidelity(tmp_path):
    outcome = CallOutcome(
        case="missing_required", property_name="path", outcome="graceful_error",
        detail="ValidationError: path is required", arguments={"other_field": "x"},
        started_at=1000.0, duration_ms=12.5,
    )
    report = FuzzReport(server_command="x", tools=[
        ToolResult(name="read_file", tested=True, outcomes=[outcome]),
    ])
    out = tmp_path / "trace.jsonl"
    write_jsonl_trace(report, str(out))

    lines = _read_lines(out)[1:]
    assert lines == [{
        "event": "tool.call",
        "tool": "read_file",
        "case": "missing_required",
        "property": "path",
        "outcome": "graceful_error",
        "detail": "ValidationError: path is required",
        "arguments": {"other_field": "x"},
        "startedAt": 1000.0 * 1000,
        "durationMs": 12.5,
    }]


def test_ok_outcomes_are_written_too_unlike_the_scored_report(tmp_path):
    # The whole point of this module: report.to_dict() would drop this
    # entirely (only crashes/timeouts/valid_call_issue survive there).
    outcome = CallOutcome(case="valid", property_name=None, outcome="ok", arguments={"a": 1})
    report = FuzzReport(server_command="x", tools=[
        ToolResult(name="t", tested=True, outcomes=[outcome]),
    ])
    out = tmp_path / "trace.jsonl"
    write_jsonl_trace(report, str(out))

    lines = _read_lines(out)[1:]
    assert len(lines) == 1
    assert lines[0]["outcome"] == "ok"
