"""M6.7 slice 5 — Regression thresholds block rollout without auto-promoting.

evaluate_regression compares a candidate cohort's metrics to a baseline cohort's
under rollout gates. A breach BLOCKS rollout; beating the baseline NEVER
auto-promotes (verdict.promotes is always False). Acceptance item 5.
"""
from __future__ import annotations

from crewspace.application.benchmarks import evaluate_regression, run_benchmark
from crewspace.dto.benchmarks import (
    BenchmarkFixture,
    BenchmarkRunSpec,
    BenchmarkSuite,
    BenchmarkToolSpec,
    BenchmarkVerificationSpec,
    RegressionThreshold,
    RegressionVerdict,
)


def _fx(fixture_id: str, specs) -> BenchmarkFixture:
    """specs: list of (status, tool_ok). success_rate = succeeded/total;
    tool_failure_rate = tool errors / total (one tool per run)."""
    runs = []
    for status, tool_ok in specs:
        runs.append(
            BenchmarkRunSpec(
                status=status,
                latency_seconds=8.0 if status == "succeeded" else 2.0,
                tools=(BenchmarkToolSpec(status="ok" if tool_ok else "error", duration_ms=150 if tool_ok else 10),),
                verification=(BenchmarkVerificationSpec(status="passed" if tool_ok else "failed"),),
                change_set_status="reviewed" if status == "succeeded" else "captured",
            )
        )
    return BenchmarkFixture(fixture_id=fixture_id, task="Refactor X", agent_id="agent_planner",
                             model_version="m", recorded_at="2026-08-25T00:00:00Z", runs=tuple(runs))


def test_clear_when_candidate_within_tolerance():
    base_specs = [("succeeded", True), ("succeeded", True), ("succeeded", True), ("failed", False)]
    baseline = run_benchmark(_fx("base", base_specs))
    # identical candidate -> within any tolerance
    candidate = run_benchmark(_fx("cand", base_specs))
    thresholds = [
        RegressionThreshold(metric_id="success_rate", higher_is_better=True, allowed_regression_ratio=0.05),
        RegressionThreshold(metric_id="tool_failure_rate", higher_is_better=False, allowed_regression_ratio=0.05),
    ]
    v = evaluate_regression(baseline, candidate, thresholds)
    assert isinstance(v, RegressionVerdict)
    assert v.blocks is False
    assert v.breaches == ()
    assert v.promotes is False  # evaluation NEVER promotes


def test_blocks_when_higher_is_better_metric_regresses_past_tolerance():
    baseline = run_benchmark(_fx("base", [("succeeded", True), ("succeeded", True), ("succeeded", True), ("failed", False)]))
    candidate = run_benchmark(_fx("cand", [("succeeded", True), ("succeeded", True), ("failed", False), ("failed", False)]))
    thresholds = [RegressionThreshold(metric_id="success_rate", higher_is_better=True, allowed_regression_ratio=0.05)]
    v = evaluate_regression(baseline, candidate, thresholds)
    assert v.blocks is True
    assert "success_rate" in v.breaches
    assert v.promotes is False  # a block still never promotes


def test_blocks_when_lower_is_better_metric_regresses_up():
    baseline = run_benchmark(_fx("base", [("succeeded", True), ("succeeded", True), ("succeeded", True), ("failed", False)]))
    candidate = run_benchmark(_fx("cand", [("succeeded", True), ("succeeded", True), ("failed", False), ("failed", False)]))
    thresholds = [RegressionThreshold(metric_id="tool_failure_rate", higher_is_better=False, allowed_regression_ratio=0.05)]
    v = evaluate_regression(baseline, candidate, thresholds)
    assert v.blocks is True
    assert "tool_failure_rate" in v.breaches
    assert v.promotes is False


def test_beating_baseline_does_not_auto_promote():
    baseline = run_benchmark(_fx("base", [("succeeded", True), ("succeeded", True), ("failed", False), ("failed", False)]))
    candidate = run_benchmark(_fx("cand", [("succeeded", True), ("succeeded", True), ("succeeded", True), ("succeeded", True)]))
    thresholds = [
        RegressionThreshold(metric_id="success_rate", higher_is_better=True, allowed_regression_ratio=0.05),
        RegressionThreshold(metric_id="tool_failure_rate", higher_is_better=False, allowed_regression_ratio=0.05),
    ]
    v = evaluate_regression(baseline, candidate, thresholds)
    assert v.blocks is False
    assert v.promotes is False  # better is NOT an auto-promotion


def test_missing_metric_is_explicit_error_not_silent_pass():
    baseline = run_benchmark(_fx("base", [("succeeded", True), ("succeeded", True), ("succeeded", True), ("failed", False)]))
    candidate = run_benchmark(_fx("cand", [("succeeded", True), ("succeeded", True), ("failed", False), ("failed", False)]))
    thresholds = [RegressionThreshold(metric_id="nonexistent_metric", higher_is_better=True)]
    try:
        evaluate_regression(baseline, candidate, thresholds)
        raise AssertionError("silent pass on missing metric")
    except KeyError:
        pass
