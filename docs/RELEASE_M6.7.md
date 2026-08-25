# Milestone M6.7 — Agent evaluation and reliability scorecards

Released: 2026-08-25 (WIB)
Tag: `milestone-m6.7` → commit `a2bec64` ("[verified] feat(M6.7): seeded benchmark POC — version comparison + regression alert (slice 7, 7/7 — M6.7 DONE)")

## Summary

M6.7 gives Crewspace a deterministic, privacy-aware way to measure agent
reliability and to compare agent/model/version cohorts **without misleading
mixes** and **without auto-promoting a winner**. Every metric is documented with
an explicit denominator and a retention note, every aggregate is auditable
(numerator/denominator carried through), and the whole stack runs on seeded,
isolated fixtures for replayable benchmarks.

## Acceptance (7/7 — DONE)

- [x] Metric definitions, denominators, and privacy/retention policy are documented.
- [x] Run/event data produces deterministic aggregate metrics.
- [x] Benchmark fixtures are replayable and isolated from production workspaces.
- [x] Scorecards compare agent implementation/model versions without misleading mixes.
- [x] Regression thresholds can block rollout without auto-promoting a winner.
- [x] UI links every aggregate to inspectable supporting runs.
- [x] Seeded benchmark POC demonstrates a version comparison and regression alert.

## What shipped (per slice)

1. **Documented metric definitions + deterministic aggregates** — `dto/metrics.py`
   (`METRIC_DEFINITIONS` with explicit denominator + privacy/retention note per
   metric, plus `MetricValue` carrying numerator/denominator) and
   `application/metrics.py` (`compute_scorecard`, a pure, order-independent function
   over `CodingRun` + `AgentToolCall` records; div-by-zero safe).
2. **Run/event data → aggregates via real queries** — `CodingRunRepository.list_for_team`
   (read-only port + SqlAlchemy impl, no schema change) and `compute_team_scorecard(uow, team_id)`
   which pulls real runs + captured change sets and feeds `compute_scorecard`, also
   aggregating `verification_pass_rate` and `change_set_approval_rate` (determinism
   proven equal between the DB path and the pure path).
3. **Replayable benchmark fixtures isolated from production** — `dto/benchmarks.py`
   (frozen `BenchmarkFixture`/`BenchmarkRunSpec`/`BenchmarkToolSpec`/`BenchmarkVerificationSpec`)
   and `application/benchmarks.py` (`materialize_fixture` builds the SAME real
   `CodingRun`/`AgentToolCall`/`VerificationResultDTO`/`StoredChangeSet` records every
   replay from a fixed epoch with synthetic bench-scoped ids; `run_benchmark` delegates
   to `compute_scorecard`). No DB or live workspace is touched.
4. **Version comparison without misleading mixes** — `BenchmarkSuite` + `compare_cohorts`
   / `rank_cohorts` / `cohort_label`. Cohorts are scored independently and attributed to
   `agent@version`; they are NEVER blended into a single average.
5. **Regression thresholds block rollout without auto-promoting** — `RegressionThreshold`
   + `RegressionVerdict` (frozen, `promotes` always `False`) and `evaluate_regression`.
   A breach yields `blocks=True` with the breached metric ids; beating the baseline never
   auto-promotes a winner; a missing metric raises `KeyError`.
6. **Scorecard UI links every aggregate to inspectable runs** — `application/scorecard_view.py`
   (`build_scorecard_view`) and `templates/scorecard.html`. Each metric carries its value
   + numerator/denominator and the ids of the supporting runs/change sets, deep-linked via
   real existing routes (`/api/coding/runs/{id}`, `/management/change-sets/{id}`). No dead links.
7. **Seeded benchmark POC** — `application/benchmark_poc.py` (`run_benchmark_poc`) materializes
   a baseline and a candidate cohort fixture and runs the whole stack
   (materialize → scorecard → compare → rank → regression) with no DB/workspace, returning a
   frozen `PocReport` with a fail-closed `RegressionVerdict`.

## Verification

- 24 M6.7 tests pass (`tests/test_scorecard*.py` + `tests/test_benchmark_*.py`) plus the
  34 M6.6 tests remain green.
- `makemigrations --check` clean — all new modules are pure DTO/logic with no sqlalchemy
  import, so the schema never drifts from the scorecard layer.
- `compileall`, `git diff --check`, and the added-line security scan are clean.
- Independent fail-closed review (in-process executable checks): BLOCKERS none on every slice.

## Notes / non-goals

- The scorecard is a read-only projection + evaluation surface. Promotion/rollout is an
  explicit, separate decision — evaluation can only block, never promote.
- Benchmark fixtures are self-contained; replay does not read production workspaces, so
  results are reproducible and safe to publish.
- M6.8 (Operational inbox) has started separately on `master` (1/7) and is NOT part of
  this tag.
