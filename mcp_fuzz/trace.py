"""Optional full-fidelity JSONL export of a fuzz run, for feeding a real
session into an external evidence/trajectory tool (an agent-trajectory
debugger, a log-ingest pipeline, ad hoc analysis).

The scored --json report (mcp_fuzz.report.to_dict) is deliberately narrow:
it exists to answer "what's the crash-resilience score," so it keeps only
crashes/timeouts/the one valid-call issue and drops every "ok" outcome,
the call arguments, and any timing. That's the right shape for the score,
but the wrong shape for reconstructing what the run actually did call by
call — this module writes the full FuzzReport instead, one JSON line per
tool-call outcome (plus one line per skipped tool), so nothing is lost.
"""

from __future__ import annotations

import json

from mcp_fuzz.engine import FuzzReport


def write_jsonl_trace(report: FuzzReport, path: str) -> None:
    with open(path, "w") as fh:
        fh.write(json.dumps({
            "event": "session.meta",
            "serverCommand": report.server_command,
            "connectError": report.connect_error,
        }) + "\n")

        for tool in report.tools:
            if not tool.tested:
                fh.write(json.dumps({
                    "event": "tool.skipped",
                    "tool": tool.name,
                    "skipReason": tool.skip_reason,
                }) + "\n")
                continue

            for outcome in tool.outcomes:
                fh.write(json.dumps({
                    "event": "tool.call",
                    "tool": tool.name,
                    "case": outcome.case,
                    "property": outcome.property_name,
                    "outcome": outcome.outcome,
                    "detail": outcome.detail,
                    "arguments": outcome.arguments,
                    "startedAt": outcome.started_at * 1000,  # ms, matching common log-ingest timestamp conventions
                    "durationMs": outcome.duration_ms,
                }) + "\n")
