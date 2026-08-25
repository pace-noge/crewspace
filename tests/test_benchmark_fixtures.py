"""M6.7 slice 3 — Replayable benchmark fixtures isolated from production workspaces.

A BenchmarkFixture is a self-contained, frozen task definition. materialize_fixture
turns it into the same scorecard input records every replay (runs, tool calls,
verification results, change sets) WITHOUT touching a DB or a live workspace, so
run_benchmark(fixture) is deterministic and isolated. Acceptance item 3.
"""
from __future__ import annotations

from crewspace.application.benchmarks import materialize_fixture, run_benchmark
from crewspace.dto.benchmarks import BenchmarkFixture, BenchmarkRunSpec
from crewspace.dto.metrics import METRIC_DEFINITIONS


def _fixture() -> BenchmarkFixture:
    from crewspace.dto.benchmarks import BenchmarkToolSpec, BenchmarkVerificationSpec

    return BenchmarkFixture(
        fixture_id="bench_planner_v1",
        task="Refactor module X",
        agent_id="agent_planner",
        model_version="model-a@1",
        recorded_at="2026-08-25T00:00:00Z",
        runs=(
            BenchmarkRunSpec(status="succeeded", latency_seconds=10.0,
                             tools=(BenchmarkToolSpec(status="ok", duration_ms=120),
                                    BenchmarkToolSpec(status="error", duration_ms=900)),
                             verification=(BenchmarkVerificationSpec(status="passed"),
                                           BenchmarkVerificationSpec(status="passed")),
                             change_set_status="reviewed"),
            BenchmarkRunSpec(status="succeeded", latency_seconds=30.0,
                             tools=(BenchmarkToolSpec(status="ok", duration_ms=200),),
                             verification=(BenchmarkVerificationSpec(status="passed"),),
                             change_set_status="reviewed"),
            BenchmarkRunSpec(status="failed", latency_seconds=5.0,
                             tools=(BenchmarkToolSpec(status="error", duration_ms=500),),
                             verification=(BenchmarkVerificationSpec(status="failed"),),
                             change_set_status="captured"),
            BenchmarkRunSpec(status="cancelled", latency_seconds=0.0,
                             tools=(), verification=(), change_set_status=None),
        ),
    )


def test_benchmark_fixture_is_frozen_and_forbids_extra():
    f = _fixture()
    assert f.fixture_id == "bench_planner_v1"
    try:
        f.fixture_id = "x"  # type: ignore[misc]
        raise AssertionError("fixture is mutable")
    except Exception:
        pass
    try:
        BenchmarkFixture(fixture_id="z", task="t", agent_id="a",
                         model_version="m", recorded_at="t", runs=(),
                         bogus=1)  # type: ignore[call-arg]
        raise AssertionError("extra field accepted")
    except Exception:
        pass


def test_materialize_is_isolated_and_deterministic():
    f = _fixture()
    # No DB / workspace argument required -> isolated by construction.
    runs1, tools1, vr1, cs1 = materialize_fixture(f)
    runs2, tools2, vr2, cs2 = materialize_fixture(f)
    # identical across replays (same fixture -> same inputs)
    assert [r.status for r in runs1] == [r.status for r in runs2]
    assert len(tools1) == len(tools2) == 4  # 2+1+1+0 tool calls across runs
    assert len(vr1) == len(vr2) == 4  # 2+1+1+0 = 4 verification results
    assert len(cs1) == len(cs2) == 3  # 3 runs with change_set_status (cancelled has None)


def test_run_benchmark_is_replayable_and_maps_to_documented_metrics():
    f = _fixture()
    a = run_benchmark(f)
    b = run_benchmark(f)
    assert a == b  # replayable: identical metrics every time
    # 4 runs total: 2 succeeded, 1 failed, 1 cancelled
    assert a["success_rate"].numerator == 2 and a["success_rate"].denominator == 4
    assert a["failure_rate"].numerator == 1
    assert a["cancellation_rate"].numerator == 1
    # latency: (10+30+5+0)/4 = 11.25 (cancelled has 0s and both timestamps set)
    assert a["mean_latency_seconds"].value == 11.25
    # 4 tool calls: 2 errors / 4
    assert a["tool_failure_rate"].numerator == 2 and a["tool_failure_rate"].denominator == 4
    # 4 verification: 3 passed / 4
    assert a["verification_pass_rate"].numerator == 3 and a["verification_pass_rate"].denominator == 4
    # 3 change sets: 2 reviewed / 3
    assert a["change_set_approval_rate"].numerator == 2 and a["change_set_approval_rate"].denominator == 3
    # all emitted metric ids are documented
    for k in a:
        assert k in {m.metric_id for m in METRIC_DEFINITIONS}
