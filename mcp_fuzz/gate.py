"""Runtime counterpart to the latency/response-size checks in `report.py`:
applies the same two-signal design (an absolute threshold, plus a relative
outlier check once there's enough of a sample) to real calls an agent makes
during a live session, instead of one synthetic call per tool in a one-shot
batch report.

One real design difference from the batch version, not just a mechanical
port: `report.py` compares one tool's single call against every *other*
tool's single call on the same server, because that's all a one-shot audit
ever has — each tool called exactly once. A live session calls the same
tool many times with different real arguments, so the more meaningful
comparison is a tool against *its own* call history, not against unrelated
tools it happens to share a server with. `LatencyGate` tracks that history
per tool across the session.

Same scope discipline as `mcp_reality_check.gate`: this answers "is this
call unusually slow or bloated for this tool," not a security question —
doesn't overlap the runtime-security-proxy space either.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field

from mcp import ClientSession, types

DEFAULT_TIMEOUT_SECONDS = 15.0

LATENCY_ABSOLUTE_SLOW_MS = 5000.0
LATENCY_OUTLIER_MULTIPLIER = 3.0
MIN_CALLS_FOR_LATENCY_OUTLIER = 3
LATENCY_OUTLIER_MIN_MS = 100.0  # see report.py: a relative outlier must also cross this floor

RESPONSE_SIZE_ABSOLUTE_CHARS = 20000
RESPONSE_SIZE_OUTLIER_MULTIPLIER = 3.0
MIN_CALLS_FOR_RESPONSE_SIZE_OUTLIER = 3
RESPONSE_SIZE_OUTLIER_MIN_CHARS = 1000


def _median(values: list[float]) -> float:
    values = sorted(values)
    mid = len(values) // 2
    if len(values) % 2 == 1:
        return values[mid]
    return (values[mid - 1] + values[mid]) / 2


def _response_text(result) -> str:
    if not isinstance(result, types.CallToolResult):
        return ""
    return "".join(c.text for c in result.content if isinstance(c, types.TextContent))


@dataclass
class LatencyResult:
    tool_name: str
    # "ok" | "crash" | "timeout" — same meaning as mcp_reality_check.gate's
    # GateResult.outcome; no duration/size to judge without a real response.
    outcome: str
    detail: str = ""
    duration_ms: float = 0.0
    response_chars: int = 0
    slow_reasons: list[str] = field(default_factory=list)
    bloated_reasons: list[str] = field(default_factory=list)

    @property
    def flagged(self) -> bool:
        return bool(self.slow_reasons or self.bloated_reasons)


class LatencyGate:
    """Stateful across a session: call `timed_call` once per real tool call
    an agent makes, on the same instance, so later calls to a given tool
    are judged against that tool's own accumulating history."""

    def __init__(
        self,
        slow_threshold_ms: float = LATENCY_ABSOLUTE_SLOW_MS,
        latency_outlier_multiplier: float = LATENCY_OUTLIER_MULTIPLIER,
        min_calls_for_latency_outlier: int = MIN_CALLS_FOR_LATENCY_OUTLIER,
        latency_outlier_min_ms: float = LATENCY_OUTLIER_MIN_MS,
        bloat_threshold_chars: int = RESPONSE_SIZE_ABSOLUTE_CHARS,
        size_outlier_multiplier: float = RESPONSE_SIZE_OUTLIER_MULTIPLIER,
        min_calls_for_size_outlier: int = MIN_CALLS_FOR_RESPONSE_SIZE_OUTLIER,
        size_outlier_min_chars: int = RESPONSE_SIZE_OUTLIER_MIN_CHARS,
    ):
        self.slow_threshold_ms = slow_threshold_ms
        self.latency_outlier_multiplier = latency_outlier_multiplier
        self.min_calls_for_latency_outlier = min_calls_for_latency_outlier
        self.latency_outlier_min_ms = latency_outlier_min_ms
        self.bloat_threshold_chars = bloat_threshold_chars
        self.size_outlier_multiplier = size_outlier_multiplier
        self.min_calls_for_size_outlier = min_calls_for_size_outlier
        self.size_outlier_min_chars = size_outlier_min_chars
        self._durations: dict[str, list[float]] = {}
        self._sizes: dict[str, list[int]] = {}

    def record(self, tool_name: str, duration_ms: float, response_chars: int) -> LatencyResult:
        result = LatencyResult(tool_name, "ok", duration_ms=duration_ms, response_chars=response_chars)

        history_d = self._durations.setdefault(tool_name, [])
        if duration_ms > self.slow_threshold_ms:
            result.slow_reasons.append(
                f"{duration_ms:.0f}ms, over the {self.slow_threshold_ms:.0f}ms absolute threshold"
            )
        if len(history_d) >= self.min_calls_for_latency_outlier:
            median = _median(history_d)
            if median > 0 and duration_ms > max(self.latency_outlier_multiplier * median, self.latency_outlier_min_ms):
                result.slow_reasons.append(
                    f"{duration_ms / median:.1f}x this tool's own median so far ({median:.0f}ms, {len(history_d)} prior calls)"
                )
        history_d.append(duration_ms)

        history_s = self._sizes.setdefault(tool_name, [])
        if response_chars > self.bloat_threshold_chars:
            result.bloated_reasons.append(
                f"{response_chars:,} chars (~{response_chars // 4:,} est. tokens), "
                f"over the {self.bloat_threshold_chars:,}-char absolute threshold"
            )
        if len(history_s) >= self.min_calls_for_size_outlier:
            median = _median(history_s)
            if median > 0 and response_chars > max(self.size_outlier_multiplier * median, self.size_outlier_min_chars):
                result.bloated_reasons.append(
                    f"{response_chars / median:.1f}x this tool's own median so far ({median:.0f} chars, {len(history_s)} prior calls)"
                )
        history_s.append(response_chars)

        return result

    async def timed_call(
        self,
        session: ClientSession,
        tool_name: str,
        arguments: dict,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
    ) -> LatencyResult:
        """Calls `tool_name` via `session`, times it, and records the
        result against this gate's per-tool history. Use in place of a bare
        `session.call_tool(...)` to get slow/bloated-relative-to-usual
        flagging on every real call, not just a one-shot audit."""
        started = time.monotonic()
        try:
            result = await asyncio.wait_for(
                session.call_tool(tool_name, arguments), timeout=timeout
            )
        except asyncio.TimeoutError:
            return LatencyResult(tool_name, "timeout", detail=f"no response within {timeout}s")
        except Exception as exc:
            return LatencyResult(tool_name, "crash", detail=f"{type(exc).__name__}: {exc}")

        duration_ms = (time.monotonic() - started) * 1000
        response_chars = len(_response_text(result))
        return self.record(tool_name, duration_ms, response_chars)
