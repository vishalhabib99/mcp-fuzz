from __future__ import annotations

import argparse
import asyncio
import json
import sys

from mcp_fuzz.engine import DEFAULT_TIMEOUT_SECONDS, run_fuzz
from mcp_fuzz.report import build_report, render_text, to_dict


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
        "--timeout", type=float, default=DEFAULT_TIMEOUT_SECONDS,
        help=f"seconds to wait for a single tool call before treating it as a hang (default {DEFAULT_TIMEOUT_SECONDS})",
    )
    parser.add_argument("--json", action="store_true", help="emit machine-readable JSON instead of text")
    parser.add_argument(
        "--fail-under", type=float, default=None,
        help="exit non-zero if the crash-resilience percent is below this threshold",
    )
    args = parser.parse_args()

    command_parts = [c for c in args.command if c != "--"]
    if not command_parts:
        parser.error("no server command given — e.g. `mcp-fuzz -- python server.py`")

    command, *rest = command_parts
    raw = asyncio.run(run_fuzz(
        command=command,
        args=rest,
        include_destructive=args.include_destructive,
        timeout=args.timeout,
    ))
    report = build_report(raw)

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


if __name__ == "__main__":
    main()
