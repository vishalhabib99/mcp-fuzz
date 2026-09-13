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
        "delete_everything", "always_crashes", "kills_process", "slow_but_fine",
        "bloated_but_fine", "not_concurrency_safe",
        "create_item", "get_item", "delete_item",
        "create_ticket", "get_ticket", "delete_ticket",
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
    # get_item/get_ticket are the same documented synthetic-input false
    # positive as everywhere else in this project: their schema-derived
    # "valid" call uses a placeholder id string that was never actually
    # created via create_item/create_ticket in this (non-sequential) run,
    # so the real, correct-behavior lookup miss surfaces as valid_call_errored.
    flagged = [t for t in report.tools if t.valid_call_issue is not None]
    assert {t.name for t in flagged} == {"hangs_forever", "always_crashes", "kills_process", "get_item", "get_ticket"}


ENV_REQUIRED_SERVER = str(Path(__file__).parent / "fixtures" / "env_required_server.py")


def test_env_kwarg_is_passed_through_to_the_target_server():
    # Verified against a real failure: brave/brave-search-mcp-server refuses
    # to start at all without BRAVE_API_KEY set. Without this working, the
    # only way to fuzz any API-key-gated server would be if that key
    # happened to already be in the SDK's own safe default allowlist, which
    # by design it never is.
    import asyncio

    report = asyncio.run(run_fuzz(
        sys.executable, [ENV_REQUIRED_SERVER],
        env={"REQUIRED_TEST_KEY": "expected-value"}, timeout=TIMEOUT,
    ))
    assert report.connect_error is None


def test_no_env_kwarg_does_not_leak_or_guess_the_required_value():
    # The flip side: without an explicit env, the server must NOT start —
    # confirms this isn't accidentally inheriting the operator's full shell
    # environment (which would be a real secret-leaking regression), only
    # the SDK's own minimal safe default (PATH, HOME, ...).
    import asyncio

    report = asyncio.run(run_fuzz(sys.executable, [ENV_REQUIRED_SERVER], timeout=TIMEOUT))
    assert report.connect_error is not None


def test_env_kwarg_merges_onto_safe_defaults_rather_than_replacing_them():
    # A caller passing one custom var (e.g. --env BRAVE_API_KEY=...) must not
    # lose PATH/HOME in the process — verified against a real failure mode:
    # passing only BRAVE_API_KEY with no PATH would break `npx` itself
    # before the target server ever runs, a strictly worse outcome than the
    # SDK's own default. A pure unit test on the merge itself rather than an
    # end-to-end subprocess launch, since a bare PATH-resolved command name
    # isn't portable across environments (the system `python3` on this
    # machine's PATH, for instance, isn't the one `mcp` is installed into).
    from mcp.client.stdio import get_default_environment

    from mcp_fuzz.engine import _merged_env

    merged = _merged_env({"REQUIRED_TEST_KEY": "expected-value"})
    assert merged["REQUIRED_TEST_KEY"] == "expected-value"
    for key in get_default_environment():
        assert key in merged


def test_no_env_is_passed_through_unchanged():
    # env=None/{} must not go through the merge at all — confirms `_merged_env`
    # doesn't change `run_fuzz`'s existing default behavior (the SDK's own
    # `get_default_environment()` fallback) for every caller that never
    # passes `env`, which is every caller before this option existed.
    from mcp_fuzz.engine import _merged_env

    assert _merged_env(None) is None
    assert _merged_env({}) == {}


def test_every_outcome_carries_its_own_call_arguments(fuzz_report):
    # The scored report (report.py) never needed this, so it was easy for
    # it to go unrecorded entirely — confirms the outer _timed_call wrapper
    # in run_fuzz actually attaches it to every outcome, not just some.
    tool = _tool(fuzz_report, "well_behaved")
    for outcome in tool.outcomes:
        assert isinstance(outcome.arguments, dict)
    missing = next(o for o in tool.outcomes if o.case == "missing_required")
    assert missing.property_name not in missing.arguments


def test_every_outcome_carries_real_timing(fuzz_report):
    # started_at/duration_ms come from wall-clock time.time() around the
    # real call, not a placeholder — a hanging call should show a duration
    # close to the fixture's TIMEOUT, not 0.
    tool = _tool(fuzz_report, "hangs_forever")
    valid = next(o for o in tool.outcomes if o.case == "valid")
    assert valid.outcome == "timeout"
    assert valid.started_at > 0
    assert valid.duration_ms >= TIMEOUT * 1000 * 0.9  # allow a little slack, not an exact bound


def test_skipped_tools_have_no_outcomes_but_still_carry_a_reason(fuzz_report):
    tool = _tool(fuzz_report, "delete_everything")
    assert tool.outcomes == []
    assert tool.skip_reason is not None
