"""M6.6 slice 2 — Deterministic transitions + bounded retry policy (item 2).

The pipeline state machine runs the versioned STAGE_CONTRACTS from slice 1 in a
deterministic order, gates each stage on its required inputs, caps retries, and
fails closed (terminal FAILED) when retries are exhausted — never silently
advancing a failed stage to its downstream.
"""
from __future__ import annotations

import pytest

from crewspace.application.pipeline import (
    DeliveryPipeline,
    IllegalPipelineTransition,
    PipelineStatus,
    RetryPolicy,
    StageStatus,
)


def _empty_pipeline(max_attempts: int = 3) -> DeliveryPipeline:
    return DeliveryPipeline(retry_policy=RetryPolicy(max_attempts=max_attempts))


def _attach_change_set(p: DeliveryPipeline, run_id: str = "run_coder") -> None:
    """Attach an immutable change-set evidence artifact to the running coder."""
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


def test_pipeline_progresses_in_deterministic_stage_order():
    p = _empty_pipeline()
    assert p.status == PipelineStatus.IN_PROGRESS
    assert p.eligible_stage() == "planner"

    # planner -> coder -> reviewer -> tester -> human_approval
    p.begin_stage("planner")
    assert p.stage_status["planner"] == StageStatus.RUNNING
    p.complete_stage("planner", produced=["plan", "task_spec"])
    assert p.stage_status["planner"] == StageStatus.SUCCEEDED
    assert p.produced == {"plan", "task_spec"}

    assert p.eligible_stage() == "coder"
    p.begin_stage("coder")
    _attach_change_set(p)
    p.complete_stage("coder", produced=["code", "change_set"])
    assert p.stage_status["coder"] == StageStatus.SUCCEEDED

    p.begin_stage("reviewer")
    p.complete_stage("reviewer", produced=["review"])
    p.begin_stage("tester")
    p.complete_stage("tester", produced=["verification"])

    assert p.eligible_stage() == "human_approval"
    p.begin_stage("human_approval")
    p.grant_human_approval("u_bilal", True)
    p.complete_stage("human_approval", produced=["delivery_decision"])
    assert p.stage_status["human_approval"] == StageStatus.SUCCEEDED
    assert p.status == PipelineStatus.SUCCEEDED


def test_stage_only_advances_when_required_inputs_present():
    p = _empty_pipeline()
    # Cannot start coder before planner produced the required `plan` artifact.
    with pytest.raises(IllegalPipelineTransition):
        p.begin_stage("coder")
    # Begin planner, finish it -> now coder is gated-open.
    p.begin_stage("planner")
    p.complete_stage("planner", produced=["plan", "task_spec"])
    p.begin_stage("coder")  # legal now
    assert p.stage_status["coder"] == StageStatus.RUNNING


def test_bounded_retry_policy_caps_attempts_and_fails_closed():
    p = _empty_pipeline(max_attempts=3)
    p.begin_stage("planner")
    p.complete_stage("planner", produced=["plan", "task_spec"])

    # Fail coder within the cap: retryable, pipeline still in progress.
    p.begin_stage("coder")
    p.fail_stage("coder")
    assert p.can_retry("coder") is True
    assert p.status == PipelineStatus.IN_PROGRESS
    assert p.attempts["coder"] == 1

    # Retry and fail again -> still capped, still retryable.
    p.retry_stage("coder")
    p.begin_stage("coder")
    p.fail_stage("coder")
    assert p.can_retry("coder") is True
    assert p.attempts["coder"] == 2

    # Third attempt fails -> retries exhausted -> terminal FAILED.
    p.retry_stage("coder")
    p.begin_stage("coder")
    p.fail_stage("coder")
    assert p.can_retry("coder") is False
    assert p.status == PipelineStatus.FAILED
    # Downstream stage never ran and cannot be started once terminal.
    assert p.stage_status["reviewer"] == StageStatus.PENDING
    with pytest.raises(IllegalPipelineTransition):
        p.begin_stage("reviewer")


def test_failed_stage_does_not_silently_advance_downstream():
    p = _empty_pipeline(max_attempts=2)
    p.begin_stage("planner")
    p.complete_stage("planner", produced=["plan", "task_spec"])
    p.begin_stage("coder")
    p.fail_stage("coder")  # within cap -> retryable
    # Reviewer must NOT become eligible while coder is failed.
    assert p.eligible_stage() == "coder"
    with pytest.raises(IllegalPipelineTransition):
        p.begin_stage("reviewer")
    # Successful retry then completes -> reviewer becomes eligible.
    p.retry_stage("coder")
    p.begin_stage("coder")
    _attach_change_set(p)
    p.complete_stage("coder", produced=["code", "change_set"])
    assert p.eligible_stage() == "reviewer"


def test_cancelled_pipeline_blocks_transitions():
    p = _empty_pipeline()
    p.begin_stage("planner")
    p.cancel()
    assert p.status == PipelineStatus.CANCELLED
    with pytest.raises(IllegalPipelineTransition):
        p.begin_stage("coder")
    with pytest.raises(IllegalPipelineTransition):
        p.complete_stage("planner", produced=["plan"])


def test_cannot_complete_a_stage_that_is_not_running():
    p = _empty_pipeline()
    with pytest.raises(IllegalPipelineTransition):
        p.complete_stage("planner", produced=["plan"])


def test_pipeline_rejects_broken_contract_graph():
    from crewspace.dto.handoffs import HandoffContract

    broken = {
        "planner": HandoffContract(schema_version="1.0", required_inputs=frozenset(), produces=frozenset({"plan"})),
        "coder": HandoffContract(schema_version="1.0", required_inputs=frozenset({"nope"}), produces=frozenset()),
    }
    with pytest.raises(IllegalPipelineTransition):
        DeliveryPipeline(contracts=broken)
