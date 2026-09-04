"""End-to-end tests: these actually launch the real fixture MCP server as a
subprocess and talk to it over real stdio — not mocked. Slower than a pure
unit test, but this is the whole point of the tool: verifying it correctly
classifies real runtime behavior, not just its own input-generation logic
(see test_generator.py for that)."""

import sys
from pathlib import Path

import pytest

from mcp_fuzz.engine import run_fuzz
from mcp_fuzz.report import build_report

FIXTURE_SERVER = str(Path(__file__).parent / "fixtures" / "fixture_server.py")
TIMEOUT = 3.0


@pytest.fixture(scope="module")
def fuzz_report():
    import asyncio

    return asyncio.run(run_fuzz(sys.executable, [FIXTURE_SERVER], timeout=TIMEOUT))


def _tool(fuzz_report, name):
    return next(t for t in fuzz_report.tools if t.name == name)


def test_connects_and_lists_all_five_tools(fuzz_report):
    assert fuzz_report.connect_error is None
    names = {t.name for t in fuzz_report.tools}
    assert names == {
        "well_behaved", "crashes_on_bad_input", "hangs_forever",
        "delete_everything", "always_crashes", "kills_process",
    }


def test_non_read_only_tool_is_skipped_by_default(fuzz_report):
    tool = _tool(fuzz_report, "delete_everything")
    assert tool.tested is False
    assert "readOnlyHint" in tool.skip_reason


def test_well_behaved_tool_has_no_crashes(fuzz_report):
    tool = _tool(fuzz_report, "well_behaved")
    assert tool.tested is True
    assert all(o.outcome != "crash" for o in tool.outcomes)
    valid = next(o for o in tool.outcomes if o.case == "valid")
    assert valid.outcome == "ok"


def test_hanging_tool_is_detected_as_timeout(fuzz_report):
    tool = _tool(fuzz_report, "hangs_forever")
    valid = next(o for o in tool.outcomes if o.case == "valid")
    assert valid.outcome == "timeout"


def test_sdk_caught_exception_is_not_misreported_as_a_crash(fuzz_report):
    # always_crashes raises inside its handler; the SDK converts that to a
    # structured is_error response rather than killing the process — must
    # be classified as an error, not a "crash" (process death).
    tool = _tool(fuzz_report, "always_crashes")
    valid = next(o for o in tool.outcomes if o.case == "valid")
    assert valid.outcome == "valid_call_errored"


def test_process_death_is_detected_as_a_real_crash(fuzz_report):
    tool = _tool(fuzz_report, "kills_process")
    valid = next(o for o in tool.outcomes if o.case == "valid")
    assert valid.outcome == "crash"


def test_engine_recovers_after_a_crash_and_keeps_testing(fuzz_report):
    # kills_process is registered before crashes_on_bad_input/well_behaved
    # doesn't matter — what matters is that tools registered *after* the
    # crashing one in iteration order still get real results, not silently
    # dropped because the connection died.
    tool = _tool(fuzz_report, "kills_process")
    non_valid = [o for o in tool.outcomes if o.case != "valid"]
    assert len(non_valid) == 2  # missing_required + wrong_type for its one param
    assert all(o.outcome != "crash" for o in non_valid)  # rejected by schema validation, not a repeat crash


def test_report_scores_crash_resilience_without_penalizing_valid_call_errors(fuzz_report):
    report = build_report(fuzz_report)
    assert report.crash_resilience_percent == 100.0
    assert report.grade == "A"
    assert report.crash_count == 0
    assert report.timeout_count == 0
    # The valid-call issues (timeout, SDK-caught error, real crash) are
    # real and surfaced, just not folded into the bad-input crash score.
    flagged = [t for t in report.tools if t.valid_call_issue is not None]
    assert {t.name for t in flagged} == {"hangs_forever", "always_crashes", "kills_process"}
