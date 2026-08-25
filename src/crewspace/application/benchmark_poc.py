"""M6.7 slice 7 (final) — Seeded benchmark POC orchestrator.

run_benchmark_poc materializes two cohort fixtures (a baseline model and a candidate
model) and runs the WHOLE M6.7 stack on them — materialize -> scorecard ->
compare -> rank -> regression — WITHOUT touching any production workspace or DB.
It returns a deterministic PocReport: the cohort comparison, the ranking by a metric,
and a fail-closed RegressionVerdict (blocks rollout, never auto-promotes).

This is the milestone acceptance POC: it proves the scorecard + version-comparison +
regression-alert machinery works end to end on seeded, isolated fixtures.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional

from crewspace.application.benchmarks import (
    compare_cohorts,
    evaluate_regression,
    rank_cohorts,
    run_benchmark,
)
from crewspace.dto.benchmarks import BenchmarkFixture, RegressionThreshold, RegressionVerdict


@dataclass(frozen=True)
class PocReport:
    baseline_fixture_id: str
    candidate_fixture_id: str
    cohort_scores: dict  # fixture_id -> metric dict (attributed to agent+version)
    ranking: List[tuple]  # [(fixture_id, value, cohort_label), ...] by metric_id
    verdict: RegressionVerdict
    ranked_by: str


def run_benchmark_poc(
    baseline: BenchmarkFixture,
    candidate: BenchmarkFixture,
    thresholds: List[RegressionThreshold],
    *,
    ranked_by: str = "success_rate",
) -> PocReport:
    """Compare a candidate cohort to a baseline cohort and surface a regression alert.

    Pure + deterministic + isolated: only in-memory fixtures are used. No DB, no
    live workspace, no network.
    """
    # Materialize + score each cohort independently (the fixtures are the single
    # source of truth; no production run/event data is read).
    baseline_metrics = run_benchmark(baseline)
    candidate_metrics = run_benchmark(candidate)

    # Side-by-side comparison (attributed to each cohort's agent+version, never
    # blended) and a ranking by the chosen metric.
    cohorts = compare_cohorts([baseline, candidate])
    ranking = rank_cohorts(cohorts, ranked_by, descending=True)

    # Fail-closed regression gate: candidate vs baseline. Beating the baseline never
    # auto-promotes; a breach only BLOCKS rollout.
    verdict = evaluate_regression(baseline_metrics, candidate_metrics, thresholds)

    return PocReport(
        baseline_fixture_id=baseline.fixture_id,
        candidate_fixture_id=candidate.fixture_id,
        cohort_scores=cohorts,
        ranking=ranking,
        verdict=verdict,
        ranked_by=ranked_by,
    )
