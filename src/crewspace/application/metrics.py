"""M6.7 slice 1 — Deterministic scorecard computation (pure application logic).

compute_scorecard(runs, tool_calls=...) is a pure function over CodingRun-like
and AgentToolCall-like records. It is order-independent (deterministic) and emits
MetricValue aggregates whose (numerator, denominator) match METRIC_DEFINITIONS.
No DB, no framework — unit-testable and migration-safe.
"""
from __future__ import annotations

from datetime import datetime
from typing import Iterable, Optional, Sequence

from crewspace.dto.metrics import METRIC_BY_ID, MetricValue


def _ratio(metric_id: str, numerator: int, denominator: int) -> MetricValue:
    value = (numerator / denominator) if denominator else 0.0
    return MetricValue(metric_id=metric_id, value=value, numerator=float(numerator), denominator=float(denominator))


def compute_scorecard(
    runs: Sequence[object],
    tool_calls: Optional[Iterable[object]] = None,
) -> dict[str, MetricValue]:
    """Compute deterministic aggregate reliability metrics.

    `runs` are CodingRun-like (attrs: status:str, started_at:datetime|None,
    finished_at:datetime|None). `tool_calls` are AgentToolCall-like (attrs:
    status:str, duration_ms:int|None, error:str|None).
    """
    runs = list(runs)
    total = len(runs)

    succeeded = sum(1 for r in runs if getattr(r, "status", None) == "succeeded")
    failed = sum(1 for r in runs if getattr(r, "status", None) == "failed")
    timed_out = sum(1 for r in runs if getattr(r, "status", None) == "timed_out")
    cancelled = sum(1 for r in runs if getattr(r, "status", None) == "cancelled")

    # latency only over runs that recorded both timestamps
    latencies: list[float] = []
    for r in runs:
        start = getattr(r, "started_at", None)
        fin = getattr(r, "finished_at", None)
        if isinstance(start, datetime) and isinstance(fin, datetime) and fin >= start:
            latencies.append((fin - start).total_seconds())
    latency_sum = sum(latencies)
    latency_n = len(latencies)
    mean_latency = (latency_sum / latency_n) if latency_n else 0.0

    tools = list(tool_calls or [])
    tool_total = len(tools)
    tool_errors = sum(1 for t in tools if getattr(t, "status", None) == "error")
    durations = [d for t in tools if isinstance((d := getattr(t, "duration_ms", None)), (int, float)) and d is not None]
    duration_sum = sum(durations)
    duration_n = len(durations)
    mean_duration = (duration_sum / duration_n) if duration_n else 0.0

    return {
        "success_rate": _ratio("success_rate", succeeded, total),
        "failure_rate": _ratio("failure_rate", failed, total),
        "timeout_rate": _ratio("timeout_rate", timed_out, total),
        "cancellation_rate": _ratio("cancellation_rate", cancelled, total),
        "mean_latency_seconds": MetricValue(
            metric_id="mean_latency_seconds",
            value=mean_latency,
            numerator=latency_sum,
            denominator=float(latency_n),
        ),
        "tool_failure_rate": _ratio("tool_failure_rate", tool_errors, tool_total),
        "tool_mean_duration_ms": MetricValue(
            metric_id="tool_mean_duration_ms",
            value=mean_duration,
            numerator=float(duration_sum),
            denominator=float(duration_n),
        ),
    }
