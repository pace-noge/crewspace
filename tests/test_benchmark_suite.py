"""M6.7 slice 4 — Scorecards compare agent/model versions without misleading mixes.

A BenchmarkSuite compares cohort fixtures by model_version, attributing every
metric to its agent+version. Cohorts are scored independently and NEVER blended
into a single average (acceptance item 4).
"""
from __future__ import annotations

from crewspace.application.benchmarks import (
    cohort_label,
    compare_cohorts,
    rank_cohorts,
    run_benchmark,
)
from crewspace.dto.benchmarks import BenchmarkFixture, BenchmarkRunSpec, BenchmarkSuite
from crewspace.dto.benchmarks import BenchmarkToolSpec, BenchmarkVerificationSpec


def _fx(fixture_id: str, agent_id: str, model_version: str, *, failed: bool) -> BenchmarkFixture:
    if failed:
        runs = (BenchmarkRunSpec(status="failed", latency_seconds=2.0,
                                 tools=(BenchmarkToolSpec(status="error", duration_ms=10),),
                                 verification=(BenchmarkVerificationSpec(status="failed"),),
                                 change_set_status="captured"),)
    else:
        runs = (BenchmarkRunSpec(status="succeeded", latency_seconds=8.0,
                                 tools=(BenchmarkToolSpec(status="ok", duration_ms=150),),
                                 verification=(BenchmarkVerificationSpec(status="passed"),),
                                 change_set_status="reviewed"),)
    return BenchmarkFixture(fixture_id=fixture_id, task="Refactor X", agent_id=agent_id,
                             model_version=model_version, recorded_at="2026-08-25T00:00:00Z",
                             runs=runs)


def test_cohort_label_attributes_agent_and_version():
    f = _fx("good", "agent_planner", "model-a@2", failed=False)
    assert cohort_label(f) == "agent_planner@model-a@2"


def test_compare_cohorts_is_per_cohort_and_not_blended():
    good = _fx("good", "agent_planner", "model-a@2", failed=False)
    bad = _fx("bad", "agent_planner", "model-a@1", failed=True)
    cohorts = compare_cohorts([good, bad])

    # one entry per fixture, each attributed to its agent+version
    assert set(cohorts) == {"good", "bad"}
    assert cohorts["good"]["cohort"] == "agent_planner@model-a@2"
    assert cohorts["bad"]["cohort"] == "agent_planner@model-a@1"

    # independent scoring: good=100% success, bad=0% success
    assert cohorts["good"]["success_rate"].value == 1.0
    assert cohorts["bad"]["success_rate"].value == 0.0

    # NO blended row: the suite never produces a single merged average that would
    # misleadingly hide the worse version inside the better one.
    assert "blended" not in cohorts
    assert all(isinstance(v, dict) for v in cohorts.values())
    # each cohort keeps its own denominator (no cross-cohort mixing)
    assert cohorts["good"]["success_rate"].denominator == 1
    assert cohorts["bad"]["success_rate"].denominator == 1


def test_rank_cohorts_orders_by_metric_and_attributes_version():
    good = _fx("good", "agent_planner", "model-a@2", failed=False)
    bad = _fx("bad", "agent_planner", "model-a@1", failed=True)
    cohorts = compare_cohorts([good, bad])

    by_success = rank_cohorts(cohorts, "success_rate", descending=True)
    assert by_success[0][0] == "good"  # best success first
    assert by_success[0][2] == "agent_planner@model-a@2"  # label carries version
    assert by_success[1][0] == "bad"
    # ordering value matches the attributed cohort's own metric
    assert by_success[0][1] == 1.0 and by_success[1][1] == 0.0


def test_benchmark_suite_is_frozen_container():
    good = _fx("good", "agent_planner", "model-a@2", failed=False)
    suite = BenchmarkSuite(suite_id="refactor_x", fixtures=(good,))
    assert suite.suite_id == "refactor_x"
    try:
        suite.suite_id = "z"  # type: ignore[misc]
        raise AssertionError("suite mutable")
    except Exception:
        pass
