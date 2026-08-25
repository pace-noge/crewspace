"""M6.7 slice 3 — Benchmark materialization + replay (pure application logic).

materialize_fixture(fixture) turns a frozen BenchmarkFixture into the SAME
scorecard input records on every replay (no DB, no live workspace touched), so
run_benchmark(fixture) is deterministic and isolated. The fixture's declared run
outcomes are the single source of truth — replaying it can never read or perturb
production data.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Tuple

from crewspace.application.metrics import compute_scorecard
from crewspace.domain.entities import AgentToolCall, CodingRun, StoredChangeSet
from crewspace.dto.benchmarks import BenchmarkFixture, RegressionThreshold, RegressionVerdict
from crewspace.dto.change_sets import VerificationResultDTO

# Fixed epoch so materialized timestamps are deterministic across replays.
_FIXTURE_EPOCH = datetime(2026, 1, 1, 0, 0, 0)
_BENCH_TEAM = "bench"
_BENCH_REPO = "bench"


def materialize_fixture(fixture: BenchmarkFixture) -> Tuple[list, list, list, list]:
    """Return (runs, tool_calls, verification_results, change_sets) for the fixture.

    Records are real domain/dto instances with synthetic benchmark-scoped ids,
    built purely from the fixture's declared outcomes. Deterministic and isolated.
    """
    runs: list = []
    tool_calls: list = []
    verification_results: list = []
    change_sets: list = []

    for i, spec in enumerate(fixture.runs):
        run_id = f"{fixture.fixture_id}__run{i}"
        start = _FIXTURE_EPOCH
        fin = _FIXTURE_EPOCH + timedelta(seconds=spec.latency_seconds)
        runs.append(
            CodingRun(
                id=run_id,
                team_id=_BENCH_TEAM,
                repository_id=_BENCH_REPO,
                requested_by="bench",
                agent_id=fixture.agent_id,
                request_id=f"{run_id}__req",
                instruction=fixture.task,
                status=spec.status,
                created_at=start,
                updated_at=fin,
                started_at=start,
                finished_at=fin,
            )
        )
        for j, tool in enumerate(spec.tools):
            tool_calls.append(
                AgentToolCall(
                    id=f"{run_id}__tool{j}",
                    agent_id=fixture.agent_id,
                    initiator_id=None,
                    provider_type="native",
                    provider_id=_BENCH_REPO,
                    tool_name="bench_tool",
                    status=tool.status,
                    arguments_redacted="",
                    result_summary=None,
                    error=None,
                    duration_ms=tool.duration_ms,
                    created_at=start,
                )
            )
        for v in spec.verification:
            verification_results.append(
                VerificationResultDTO(name="bench", status=v.status, summary="")
            )
        if spec.change_set_status is not None:
            change_sets.append(
                StoredChangeSet(
                    id=f"{run_id}__cs",
                    team_id=_BENCH_TEAM,
                    repository_id=_BENCH_REPO,
                    run_id=run_id,
                    agent_id=fixture.agent_id,
                    request_id=f"{run_id}__req",
                    status=spec.change_set_status,
                    payload={},
                    created_at=start,
                )
            )

    return runs, tool_calls, verification_results, change_sets


def run_benchmark(fixture: BenchmarkFixture) -> dict:
    """Replay a fixture to its deterministic scorecard (isolated from production)."""
    runs, tool_calls, verification_results, change_sets = materialize_fixture(fixture)
    return compute_scorecard(
        runs,
        tool_calls=tool_calls,
        verification_results=verification_results,
        change_sets=change_sets,
    )


def cohort_label(fixture: BenchmarkFixture) -> str:
    """Stable attribution label: which agent + model version produced the cohort."""
    return f"{fixture.agent_id}@{fixture.model_version}"


def compare_cohorts(fixtures: list[BenchmarkFixture]) -> dict:
    """Return per-fixture scorecards, each attributed to its agent+model version.

    Cohorts are NEVER blended into a single average — comparing versions must not
    mislead by mixing a worse cohort's runs into a better one's denominator. Each
    fixture is scored independently from its own declared outcomes. The returned
    dict is keyed by fixture_id; values carry the cohort label in `cohort`.
    """
    cohorts: dict = {}
    for f in fixtures:
        metrics = run_benchmark(f)
        cohorts[f.fixture_id] = {**metrics, "cohort": cohort_label(f)}
    return cohorts


def rank_cohorts(cohorts: dict, metric_id: str, *, descending: bool = True) -> list:
    """Order cohort fixture_ids by a metric value (attributed to agent+version).

    Returns [(fixture_id, value, cohort_label), ...] sorted by the metric. This
    makes version comparison explicit and attributable — it never fabricates a
    blended row.
    """
    rows = []
    for fixture_id, data in cohorts.items():
        metric = data.get(metric_id)
        if metric is None:
            raise KeyError(f"metric {metric_id} not present in cohort {fixture_id}")
        rows.append((fixture_id, metric.value, data.get("cohort", fixture_id)))
    rows.sort(key=lambda r: r[1], reverse=descending)
    return rows


def evaluate_regression(
    baseline: dict, candidate: dict, thresholds: list[RegressionThreshold]
) -> RegressionVerdict:
    """Compare a candidate cohort to a baseline cohort under rollout gates.

    For each threshold, a "higher_is_better" metric must stay within
    baseline*(1 - allowed_regression_ratio); a "lower_is_better" metric must stay
    within baseline*(1 + allowed_regression_ratio). Any breach is collected.

    Fail-closed: the verdict can ONLY block (halt rollout). `promotes` is always
    False — beating the baseline never auto-promotes a winner; promotion is an
    explicit, separate rollout decision. A missing metric on either side is an
    explicit KeyError (never a silent pass).
    """
    breaches: list[str] = []
    for t in thresholds:
        base = baseline.get(t.metric_id)
        cand = candidate.get(t.metric_id)
        if base is None or cand is None:
            raise KeyError(f"metric {t.metric_id} missing from baseline/candidate")
        bv = base.value if hasattr(base, "value") else float(base)
        cv = cand.value if hasattr(cand, "value") else float(cand)
        if t.higher_is_better:
            floor = bv * (1.0 - t.allowed_regression_ratio)
            if cv < floor - 1e-9:
                breaches.append(t.metric_id)
        else:
            ceil = bv * (1.0 + t.allowed_regression_ratio)
            if cv > ceil + 1e-9:
                breaches.append(t.metric_id)
    return RegressionVerdict(blocks=bool(breaches), breaches=tuple(breaches), promotes=False)
