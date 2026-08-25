"""M6.7 slice 1 — Metric definitions + deterministic aggregate scorecard.

Acceptance item 1 (metric definitions, denominators, privacy/retention policy are
documented) and item 2 (run/event data produces deterministic aggregate metrics).
The scorecard is a PURE function over CodingRun + AgentToolCall records (no DB),
so it is unit-testable and migration-safe. Each metric carries an explicit
documented denominator.
"""
from __future__ import annotations

from datetime import datetime, timedelta

from crewspace.application.metrics import compute_scorecard
from crewspace.dto.metrics import METRIC_DEFINITIONS, ScorecardMetric


def _run(status: str, started: datetime | None = None, finished: datetime | None = None) -> object:
    """Minimal CodingRun-like object with the fields compute_scorecard reads."""

    class _R:
        pass

    r = _R()
    r.status = status
    r.started_at = started
    r.finished_at = finished
    return r


def _tool(status: str, duration_ms: int | None = None, error: str | None = None) -> object:
    class _T:
        pass

    t = _T()
    t.status = status
    t.duration_ms = duration_ms
    t.error = error
    return t


def test_metric_definitions_are_documented_with_denominators():
    # Every metric must declare an explicit denominator + privacy/retention note.
    assert METRIC_DEFINITIONS, "no metric definitions registered"
    for m in METRIC_DEFINITIONS:
        assert isinstance(m, ScorecardMetric)
        assert m.metric_id and m.label
        assert m.denominator, f"{m.metric_id} has no documented denominator"
        assert m.unit in {"ratio", "count", "ms", "seconds"}
        assert m.privacy, f"{m.metric_id} has no privacy/retention note"


def test_scorecard_is_deterministic_across_orderings():
    base = datetime(2026, 8, 25, 0, 0, 0)
    runs = [
        _run("succeeded", base, base + timedelta(seconds=10)),
        _run("failed", base, base + timedelta(seconds=4)),
        _run("cancelled"),
        _run("timed_out", base, base + timedelta(seconds=30)),
        _run("succeeded", base, base + timedelta(seconds=20)),
    ]
    tools = [
        _tool("ok", duration_ms=120),
        _tool("error", duration_ms=900, error="boom"),
        _tool("ok", duration_ms=300),
    ]
    first = compute_scorecard(runs, tool_calls=tools)
    # reorder inputs -> identical aggregates (deterministic, order-independent)
    second = compute_scorecard(list(reversed(runs)), tool_calls=list(reversed(tools)))
    assert first == second


def test_scorecard_aggregates_match_documented_denominators():
    base = datetime(2026, 8, 25, 0, 0, 0)
    runs = [
        _run("succeeded", base, base + timedelta(seconds=10)),
        _run("succeeded", base, base + timedelta(seconds=30)),
        _run("failed", base, base + timedelta(seconds=5)),
        _run("cancelled"),
        _run("timed_out", base, base + timedelta(seconds=60)),
    ]
    tools = [_tool("ok", 100), _tool("error", 500, "boom"), _tool("ok", 200)]

    sc = compute_scorecard(runs, tool_calls=tools)

    # success rate = succeeded / total
    assert sc["success_rate"].numerator == 2
    assert sc["success_rate"].denominator == 5
    # failure rate = failed / total
    assert sc["failure_rate"].numerator == 1
    # timeout rate = timed_out / total
    assert sc["timeout_rate"].numerator == 1
    # cancellation rate = cancelled / total
    assert sc["cancellation_rate"].numerator == 1
    # mean latency over runs with both timestamps: (10+30+5+60)/4 = 26.25s
    assert sc["mean_latency_seconds"].value == 26.25
    assert sc["mean_latency_seconds"].denominator == 4
    # tool failure rate = error / total tools = 1/3
    assert sc["tool_failure_rate"].numerator == 1
    assert sc["tool_failure_rate"].denominator == 3
    # mean tool duration = (100+500+200)/3 = 266.666...
    assert abs(sc["tool_mean_duration_ms"].value - (800 / 3)) < 1e-9
