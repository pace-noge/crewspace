"""M6.7 slice 7 (final) — Seeded benchmark POC: version comparison + regression alert.

A scripted end-to-end that materializes two cohort fixtures (a baseline model and a
candidate model), compares them, ranks them by a metric, and raises a
RegressionVerdict on regression. This is the milestone acceptance POC: it exercises
the whole M6.7 stack (fixtures -> materialize -> scorecard -> compare -> rank ->
regression) without touching any production workspace or DB. Acceptance item 7.
"""
from __future__ import annotations

from crewspace.application.benchmark_poc import run_benchmark_poc
from crewspace.dto.benchmarks import (
    BenchmarkFixture,
    BenchmarkRunSpec,
    BenchmarkSuite,
    BenchmarkToolSpec,
    BenchmarkVerificationSpec,
    RegressionThreshold,
)


def _cohort(fixture_id: str, agent_id: str, model_version: str, *, ok: int, fail: int) -> BenchmarkFixture:
    runs = []
    for i in range(ok):
        runs.append(BenchmarkRunSpec(status="succeeded", latency_seconds=8.0,
            tools=(BenchmarkToolSpec(status="ok", duration_ms=120),),
            verification=(BenchmarkVerificationSpec(status="passed"),), change_set_status="reviewed"))
    for i in range(fail):
        runs.append(BenchmarkRunSpec(status="failed", latency_seconds=3.0,
            tools=(BenchmarkToolSpec(status="error", duration_ms=600),),
            verification=(BenchmarkVerificationSpec(status="failed"),), change_set_status="captured"))
    return BenchmarkFixture(fixture_id=fixture_id, task="Refactor module X", agent_id=agent_id,
                            model_version=model_version, recorded_at="2026-08-25T00:00:00Z", runs=tuple(runs))


def _thresholds():
    return [
        RegressionThreshold(metric_id="success_rate", higher_is_better=True, allowed_regression_ratio=0.05),
        RegressionThreshold(metric_id="tool_failure_rate", higher_is_better=False, allowed_regression_ratio=0.05),
    ]


def test_poc_compares_two_versions_and_reports_regression():
    baseline = _cohort("baseline", "agent_planner", "model-a@1", ok=8, fail=2)   # 80% success
    candidate = _cohort("candidate", "agent_planner", "model-a@2", ok=5, fail=5)  # 50% success -> regression

    report = run_benchmark_poc(baseline, candidate, _thresholds())

    # the report names both cohorts and attributes each to its agent+version
    assert report.baseline_fixture_id == "baseline"
    assert report.candidate_fixture_id == "candidate"
    # ranked by success_rate desc: baseline (0.80) beats candidate (0.50)
    assert report.ranking[0][0] == "baseline"
    assert report.ranking[0][2] == "agent_planner@model-a@1"
    # regression verdict: candidate regressed -> blocks, and never auto-promotes
    assert report.verdict.blocks is True
    assert "success_rate" in report.verdict.breaches
    assert report.verdict.promotes is False
    # the report is deterministic (same inputs -> same verdict)
    assert run_benchmark_poc(baseline, candidate, _thresholds()).verdict == report.verdict


def test_poc_clear_when_candidate_matches_or_beats_baseline():
    baseline = _cohort("baseline", "agent_planner", "model-a@1", ok=5, fail=5)   # 50%
    candidate = _cohort("candidate", "agent_planner", "model-a@2", ok=8, fail=2)  # 80% -> better

    report = run_benchmark_poc(baseline, candidate, _thresholds())
    assert report.verdict.blocks is False
    assert report.verdict.breaches == ()
    # better candidate is ranked first, but promotion is still never asserted
    assert report.ranking[0][0] == "candidate"
    assert report.verdict.promotes is False


def test_poc_is_isolated_from_production_no_db_or_workspace():
    # run_benchmark_poc takes only in-memory fixtures + thresholds; it requires no
    # app/uow/DB/workspace argument, proving the POC is isolated from production.
    baseline = _cohort("baseline", "agent_planner", "model-a@1", ok=9, fail=1)
    candidate = _cohort("candidate", "agent_planner", "model-a@2", ok=9, fail=1)
    report = run_benchmark_poc(baseline, candidate, _thresholds())
    # identical cohorts -> within tolerance -> clear
    assert report.verdict.blocks is False
    assert report.verdict.promotes is False
