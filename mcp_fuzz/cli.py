from __future__ import annotations

import argparse
import asyncio
import json
import sys

from mcp_fuzz.engine import DEFAULT_TIMEOUT_SECONDS, run_fuzz
from mcp_fuzz.report import (
    LATENCY_ABSOLUTE_SLOW_MS,
    RESPONSE_SIZE_ABSOLUTE_CHARS,
    build_report,
    render_text,
    to_dict,
)
from mcp_fuzz.trace import write_jsonl_trace


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="mcp-fuzz",
        description=(
            "Launches an MCP server over stdio and calls each read-only tool with "
            "schema-derived valid, missing-required, and wrong-type inputs to check "
            "whether it crashes, hangs, or returns a structured error."
        ),
    )
    parser.add_argument(
        "command",
        nargs=argparse.REMAINDER,
        help="the command (and its arguments) that launches the target MCP server, "
        "e.g. `mcp-fuzz -- python server.py` or `mcp-fuzz -- npx -y some-mcp-server`",
    )
    parser.add_argument(
        "--include-destructive",
        action="store_true",
        help="also test tools not annotated readOnlyHint=true. Off by default — see README's "
        "Safety section before turning this on against a server with real side effects.",
    )
    parser.add_argument(
        "--env", action="append", default=[], metavar="KEY=VALUE",
        help="pass an environment variable through to the target server (repeatable), e.g. "
        "--env BRAVE_API_KEY=... . Without this, only a safe minimal set (PATH, HOME, ...) "
        "is inherited — many real servers need an API key to start at all.",
    )
    parser.add_argument(
        "--timeout", type=float, default=DEFAULT_TIMEOUT_SECONDS,
        help=f"seconds to wait for a single tool call before treating it as a hang (default {DEFAULT_TIMEOUT_SECONDS})",
    )
    parser.add_argument("--json", action="store_true", help="emit machine-readable JSON instead of text")
    parser.add_argument(
        "--fail-under", type=float, default=None,
        help="exit non-zero if the crash-resilience percent is below this threshold",
    )
    parser.add_argument(
        "--slow-threshold-ms", type=float, default=LATENCY_ABSOLUTE_SLOW_MS,
        help=f"flag a tool's valid call as slow if it takes longer than this, in milliseconds "
        f"(default {LATENCY_ABSOLUTE_SLOW_MS:.0f})",
    )
    parser.add_argument(
        "--fail-under-latency", type=float, default=None,
        help="exit non-zero if the latency percent is below this threshold",
    )
    parser.add_argument(
        "--bloat-threshold-chars", type=int, default=RESPONSE_SIZE_ABSOLUTE_CHARS,
        help=f"flag a tool's valid call as bloated if its response is longer than this, in "
        f"characters (default {RESPONSE_SIZE_ABSOLUTE_CHARS:,})",
    )
    parser.add_argument(
        "--fail-under-response-size", type=float, default=None,
        help="exit non-zero if the response-size percent is below this threshold",
    )
    parser.add_argument(
        "--concurrency", type=int, default=0, metavar="N",
        help="for each tested tool, also launch N independent connections and call it at the "
        "same time (each its own subprocess of the target server) — a real test of concurrent "
        "access to whatever shared backend the server talks to. Off by default (extra "
        "subprocess launches per tool); a modest value like 3-5 is usually enough to surface "
        "a real race.",
    )
    parser.add_argument(
        "--fail-under-concurrency", type=float, default=None,
        help="exit non-zero if the concurrency percent is below this threshold",
    )
    parser.add_argument(
        "--full-trace", metavar="PATH", default=None,
        help="also write every call's full detail (tool, case, arguments, outcome, "
        "timing — not just crashes/timeouts) as JSONL to PATH, for feeding a real "
        "session into an external evidence/trajectory tool. The scored --json report "
        "deliberately drops this detail; this doesn't change that report at all.",
    )
    args = parser.parse_args()

    command_parts = [c for c in args.command if c != "--"]
    if not command_parts:
        parser.error("no server command given — e.g. `mcp-fuzz -- python server.py`")

    env = {}
    for pair in args.env:
        key, sep, value = pair.partition("=")
        if not sep:
            parser.error(f"--env expects KEY=VALUE, got {pair!r}")
        env[key] = value

    command, *rest = command_parts
    raw = asyncio.run(run_fuzz(
        command=command,
        args=rest,
        env=env or None,
        include_destructive=args.include_destructive,
        timeout=args.timeout,
        concurrency=args.concurrency,
    ))
    report = build_report(
        raw, slow_threshold_ms=args.slow_threshold_ms, bloat_threshold_chars=args.bloat_threshold_chars,
    )

    if args.full_trace:
        write_jsonl_trace(raw, args.full_trace)

    if args.json:
        print(json.dumps(to_dict(report), indent=2))
    else:
        print(render_text(report))

    if report.connect_error:
        sys.exit(2)
    if args.fail_under is not None and (
        report.crash_resilience_percent is None or report.crash_resilience_percent < args.fail_under
    ):
        sys.exit(1)
    if args.fail_under_latency is not None and (
        report.latency.percent is None or report.latency.percent < args.fail_under_latency
    ):
        sys.exit(1)
    if args.fail_under_response_size is not None and (
        report.response_size.percent is None or report.response_size.percent < args.fail_under_response_size
    ):
        sys.exit(1)
    if args.fail_under_concurrency is not None and (
        report.concurrency.percent is None or report.concurrency.percent < args.fail_under_concurrency
    ):
        sys.exit(1)


if __name__ == "__main__":
    main()
