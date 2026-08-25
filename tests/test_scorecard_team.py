"""M6.7 slice 2 — Run/event data produces deterministic aggregate metrics.

Two proofs:
1. Pure unit: compute_scorecard with verification_results + change_sets yields
   documented verification_pass_rate and change_set_approval_rate, deterministic
   across orderings.
2. DB-backed: compute_team_scorecard pulls REAL records via the repositories and
   returns the same deterministic aggregates as compute_scorecard fed the same
   records directly (acceptance item 2: run/event data -> deterministic metrics).
"""
from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from crewspace.application.metrics import compute_scorecard, compute_team_scorecard
from crewspace.dto.change_sets import (
    ChangeCommitDTO,
    ChangeSetDTO,
    VerificationResultDTO,
)
from crewspace.dto.metrics import METRIC_DEFINITIONS, MetricValue
from crewspace.domain.entities import (
    ChangeSetAuditEvent,
    CodingRun,
    CodingRepository,
    StoredChangeSet,
    TeamRepositoryAccess,
)


def _vr(status: "str", name: str = "pytest") -> VerificationResultDTO:  # noqa: A002
    return VerificationResultDTO(name=name, status=status, summary="ok")  # type: ignore[arg-type]


def _cs(status: str, verifications: tuple, run_id: str = "run_x") -> StoredChangeSet:
    cs = ChangeSetDTO(
        repository_id="repo_x", run_id=run_id, branch=f"crewspace/{run_id}",
        base_commit="0" * 40, head_commit="a" * 40,
        commits=(ChangeCommitDTO(sha="a" * 40, subject="x"),),
        files=(), additions=0, deletions=0, verification=verifications, artifacts=(),
    )
    return StoredChangeSet(
        id=f"cs_{status}_{run_id}", team_id="team_acme", repository_id="repo_x",
        run_id=run_id, agent_id="agent_coder", request_id="req_x",
        status=status, payload=cs.model_dump(), created_at=datetime(2026, 8, 25),
    )


def test_new_metrics_are_documented_and_deterministic():
    vrs = [_vr("passed"), _vr("passed"), _vr("failed")]
    csets = [_cs("reviewed", (_vr("passed"),)), _cs("captured", (_vr("failed"),))]
    a = compute_scorecard([], verification_results=vrs, change_sets=csets)
    b = compute_scorecard([], verification_results=list(reversed(vrs)), change_sets=list(reversed(csets)))
    assert a == b
    assert a["verification_pass_rate"].numerator == 2
    assert a["verification_pass_rate"].denominator == 3
    assert a["change_set_approval_rate"].numerator == 1
    assert a["change_set_approval_rate"].denominator == 2
    ids = {m.metric_id for m in METRIC_DEFINITIONS}
    assert "verification_pass_rate" in ids and "change_set_approval_rate" in ids


@pytest.mark.asyncio
async def test_compute_team_scorecard_matches_pure_over_real_records(app):
    now = datetime(2026, 8, 25, 0, 0, 0)
    base = now

    async with app.state.db.uow() as uow:
        await uow.coding_repositories.create(
            CodingRepository(id="repo_score", name="Score", default_branch="master",
                             created_by="user_bilal", created_at=now)
        )
        await uow.coding_repositories.grant_team(
            TeamRepositoryAccess(team_id="team_acme", repository_id="repo_score",
                                 granted_by="user_bilal", granted_at=now)
        )
        runs = [
            CodingRun(id="r1", team_id="team_acme", repository_id="repo_score",
                      requested_by="user_bilal", agent_id="agent_coder",
                      request_id="rq1", instruction="i", status="succeeded",
                      created_at=base, updated_at=base,
                      started_at=base, finished_at=base + timedelta(seconds=10)),
            CodingRun(id="r2", team_id="team_acme", repository_id="repo_score",
                      requested_by="user_bilal", agent_id="agent_coder",
                      request_id="rq2", instruction="i", status="failed",
                      created_at=base, updated_at=base,
                      started_at=base, finished_at=base + timedelta(seconds=5)),
            CodingRun(id="r3", team_id="team_acme", repository_id="repo_score",
                      requested_by="user_bilal", agent_id="agent_coder",
                      request_id="rq3", instruction="i", status="cancelled",
                      created_at=base, updated_at=base, started_at=None, finished_at=None),
        ]
        for r in runs:
            await uow.coding_runs.create(r)
        csets = [
            _cs("reviewed", (_vr("passed"), _vr("passed")), run_id="run_cs1"),
            _cs("reviewed", (_vr("passed"),), run_id="run_cs2"),
            _cs("captured", (_vr("failed"),), run_id="run_cs3"),
        ]
        for c in csets:
            await uow.change_sets.create(c, ChangeSetAuditEvent(
                id=f"evt_{c.id}", change_set_id=c.id, action="captured",
                actor_id="agent_coder", created_at=now))
        await uow.commit()

    async with app.state.db.uow() as uow:
        db_metrics = await compute_team_scorecard(uow, "team_acme")
        # Recompute the identical aggregates from the same records directly.
        real_runs = await uow.coding_runs.list_for_team("team_acme")
        real_csets = await uow.change_sets.list_for_teams(["team_acme"])
        vresults = []
        for s in real_csets:
            for v in s.payload.get("verification", ()):
                vresults.append(v if isinstance(v, VerificationResultDTO) else VerificationResultDTO(**v))
        pure_metrics = compute_scorecard(real_runs, verification_results=vresults, change_sets=real_csets)

    assert db_metrics == pure_metrics
    # documented aggregate values
    assert db_metrics["success_rate"].numerator == 1 and db_metrics["success_rate"].denominator == 3
    assert db_metrics["failure_rate"].numerator == 1
    assert db_metrics["cancellation_rate"].numerator == 1
    assert db_metrics["mean_latency_seconds"].value == 7.5  # (10+5)/2
    assert db_metrics["verification_pass_rate"].numerator == 3 and db_metrics["verification_pass_rate"].denominator == 4
    assert db_metrics["change_set_approval_rate"].numerator == 2 and db_metrics["change_set_approval_rate"].denominator == 3
