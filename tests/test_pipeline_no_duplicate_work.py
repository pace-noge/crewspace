"""M6.6 slice 4 — Failed/cancelled stages cannot silently advance or duplicate
downstream work (item 4).

Guards:
- Completing an already-succeeded stage is rejected (no double-emit of the same
  artifacts into downstream gating).
- A succeeded stage cannot be reopened via retry (only a FAILED stage may retry).
- Retrying a failed stage PURGES the artifacts it previously produced, so stale
  downstream work (which depended on the prior attempt) cannot advance until the
  stage is re-run and re-produces them.
- A terminal FAILED/CANCELLED pipeline rejects ALL completion/production attempts.
"""
from __future__ import annotations

import pytest

from crewspace.application.pipeline import (
    DeliveryPipeline,
    IllegalPipelineTransition,
    PipelineStatus,
    StageStatus,
    RetryPolicy,
)


def _attach_change_set(p: DeliveryPipeline, run_id: str = "run_coder") -> None:
    from crewspace.dto.handoffs import ChangeSetEvidence
    from crewspace.dto.change_sets import ChangeSetDTO, ChangeCommitDTO, GitOid

    cs = ChangeSetDTO(
        repository_id="repo_x",
        run_id=run_id,
        branch="feat/x",
        base_commit=GitOid("a" * 40),
        head_commit=GitOid("b" * 40),
        commits=(ChangeCommitDTO(sha=GitOid("c" * 40), subject="do the thing"),),
        files=(),
        additions=1,
        deletions=0,
        verification=(),
        artifacts=(),
    )
    p.attach_artifact("coder", "change_set", ChangeSetEvidence(
        producer_run_id=run_id, change_set=cs, captured_at="2026-08-25T00:00:00Z"))


def _seed(upto: str) -> DeliveryPipeline:
    p = DeliveryPipeline()
    p.begin_stage("planner")
    p.complete_stage("planner", produced=["plan", "task_spec"])
    if upto == "planner":
        return p
    p.begin_stage("coder")
    _attach_change_set(p)
    p.complete_stage("coder", produced=["code", "change_set"])
    if upto == "coder":
        return p
    p.begin_stage("reviewer")
    p.complete_stage("reviewer", produced=["review"])
    return p


def test_completing_already_succeeded_stage_is_rejected():
    p = _seed("coder")
    with pytest.raises(IllegalPipelineTransition):
        p.complete_stage("coder", produced=["code"])  # already SUCCEEDED -> double-emit blocked


def test_succeeded_stage_cannot_be_reopened_for_retry():
    p = _seed("coder")
    with pytest.raises(IllegalPipelineTransition):
        p.retry_stage("coder")  # only FAILED stages retry


def test_retry_purges_prior_artifacts_so_downstream_cannot_advance():
    p = _seed("coder")
    assert "code" in p.produced
    # Force coder into FAILED, then retry -> its produced artifacts are purged.
    p.stage_status["coder"] = StageStatus.FAILED
    p.retry_stage("coder")
    assert p.stage_status["coder"] == StageStatus.PENDING
    assert "code" not in p.produced  # stale artifact removed; downstream must re-block
    assert p.eligible_stage() == "coder"  # coder must be re-run before reviewer


def test_terminal_failed_pipeline_blocks_all_production():
    p = DeliveryPipeline(retry_policy=RetryPolicy(max_attempts=1))
    p.begin_stage("planner")
    p.complete_stage("planner", produced=["plan", "task_spec"])
    p.begin_stage("coder")
    p.fail_stage("coder")  # exhausted -> terminal FAILED
    assert p.status == PipelineStatus.FAILED
    for fn in (
        lambda: p.complete_stage("planner", produced=["plan"]),
        lambda: p.begin_stage("reviewer"),
        lambda: p.complete_stage("reviewer", produced=["review"]),
    ):
        try:
            fn()
            raise AssertionError("production allowed after terminal failure")
        except IllegalPipelineTransition:
            pass


def test_cancelled_pipeline_blocks_all_production():
    p = _seed("coder")
    p.cancel()
    assert p.status == PipelineStatus.CANCELLED
    for fn in (
        lambda: p.begin_stage("reviewer"),
        lambda: p.complete_stage("coder", produced=["code"]),
        lambda: p.complete_stage("reviewer", produced=["review"]),
    ):
        try:
            fn()
            raise AssertionError("production allowed after cancel")
        except IllegalPipelineTransition:
            pass


def test_retry_does_not_duplicate_work_after_resuccess():
    # A stage completed, failed on a later attempt, retried, and completed again
    # must leave exactly one copy of its artifacts in `produced` (set semantics +
    # purge-on-retry), never duplicated downstream gating.
    p = DeliveryPipeline(retry_policy=RetryPolicy(max_attempts=3))
    p.begin_stage("planner")
    p.complete_stage("planner", produced=["plan", "task_spec"])
    p.begin_stage("coder")
    p.complete_stage("coder", produced=["code"])
    # Simulate a downstream failure that forces coder re-run.
    p.stage_status["coder"] = StageStatus.FAILED
    p.retry_stage("coder")  # purges "code" and "change_set"
    assert "code" not in p.produced
    p.begin_stage("coder")
    _attach_change_set(p)
    p.complete_stage("coder", produced=["code", "change_set"])  # re-produces exactly once
    assert p.produced == {"plan", "task_spec", "code", "change_set"}
    assert p.eligible_stage() == "reviewer"
