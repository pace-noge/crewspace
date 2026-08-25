"""M6.6 slice 5 — Human approval required before configured delivery (item 5).

The human_approval stage is a gated, no-auto-advance decision. It cannot be
completed by default; delivery only proceeds when an explicit human grant is
supplied (fail-closed: denial/unspecified/expired blocks delivery and the
pipeline reaches a terminal state that forbids the delivery action). This
reuses the M6.5 fail-closed approval semantics: a prior denied decision must
not be overridden by a later grant attempt.
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


def _run_to_human() -> DeliveryPipeline:
    """Drive a pipeline up to the human_approval stage, evidence attached."""
    from crewspace.dto.handoffs import ChangeSetEvidence
    from crewspace.dto.change_sets import ChangeSetDTO, ChangeCommitDTO, GitOid

    p = DeliveryPipeline()
    p.begin_stage("planner")
    p.complete_stage("planner", produced=["plan", "task_spec"])
    p.begin_stage("coder")
    cs = ChangeSetDTO(repository_id="repo_x", run_id="run_coder", branch="feat/x",
        base_commit=GitOid("a" * 40), head_commit=GitOid("b" * 40),
        commits=(ChangeCommitDTO(sha=GitOid("c" * 40), subject="x"),), files=(),
        additions=1, deletions=0, verification=(), artifacts=())
    p.attach_artifact("coder", "change_set", ChangeSetEvidence(
        producer_run_id="run_coder", change_set=cs, captured_at="t"))
    p.complete_stage("coder", produced=["code", "change_set"])
    p.begin_stage("reviewer")
    p.complete_stage("reviewer", produced=["review"])
    p.begin_stage("tester")
    p.complete_stage("tester", produced=["verification"])
    assert p.eligible_stage() == "human_approval"
    p.begin_stage("human_approval")
    return p


def test_human_approval_cannot_auto_complete():
    p = _run_to_human()
    # No decision supplied -> completion is rejected; delivery never proceeds.
    with pytest.raises(IllegalPipelineTransition):
        p.complete_stage("human_approval", produced=["delivery_decision"])
    # Stage remains ineligible to advance delivery; pipeline still in progress.
    assert p.status == PipelineStatus.IN_PROGRESS
    assert p.stage_status["human_approval"] == StageStatus.RUNNING


def test_explicit_grant_advances_to_delivery_decision():
    p = _run_to_human()
    p.grant_human_approval(principal_id="u_bilal", approved=True)
    assert p.delivery_decision == "approved"
    p.complete_stage("human_approval", produced=["delivery_decision"])
    assert p.stage_status["human_approval"] == StageStatus.SUCCEEDED
    assert p.status == PipelineStatus.SUCCEEDED


def test_denial_blocks_delivery_terminal():
    p = _run_to_human()
    p.grant_human_approval(principal_id="u_bilal", approved=False)
    assert p.delivery_decision == "denied"
    # Cannot produce a delivery decision once denied; delivery stays blocked
    # (pipeline remains in progress but cannot reach a delivery decision).
    with pytest.raises(IllegalPipelineTransition):
        p.complete_stage("human_approval", produced=["delivery_decision"])
    assert p.status == PipelineStatus.IN_PROGRESS
    assert p.stage_status["human_approval"] == StageStatus.RUNNING


def test_prior_denied_decision_cannot_be_overridden_by_grant():
    # Composition with M6.5: a prior denied approval decision must fail-closed
    # block delivery even if a later grant attempt occurs.
    p = _run_to_human()
    p.set_prior_approval_decision("denied")
    # A later grant attempt must NOT override the prior denial.
    p.grant_human_approval(principal_id="u_bilal", approved=True)
    assert p.delivery_decision == "denied"  # fail-closed: prior denial wins
    with pytest.raises(IllegalPipelineTransition):
        p.complete_stage("human_approval", produced=["delivery_decision"])


def test_unsigned_or_expired_decision_blocks_delivery():
    p = _run_to_human()
    p.set_prior_approval_decision("expired")
    p.grant_human_approval(principal_id=None, approved=True)  # unsigned grant
    assert p.delivery_decision == "denied"
    with pytest.raises(IllegalPipelineTransition):
        p.complete_stage("human_approval", produced=["delivery_decision"])
