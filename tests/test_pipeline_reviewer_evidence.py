"""M6.6 slice 3 — Reviewer receives immutable change-set evidence (item 3).

The reviewer stage must consume the coder's immutable change-set (captured from
the coder's run at terminal state) rather than free-form chat. The evidence is
a frozen, tamper-evident artifact; it can only be attached while the producer
(coder) stage is running, and once the stage completes the evidence is frozen
and cannot be re-attached. The reviewer cannot begin until the change-set
evidence artifact is present.
"""
from __future__ import annotations

import pytest
from pydantic import ValidationError

from crewspace.application.pipeline import (
    DeliveryPipeline,
    IllegalPipelineTransition,
    StageStatus,
)
from crewspace.dto.handoffs import ArtifactType, ChangeSetEvidence
from crewspace.dto.change_sets import ChangeSetDTO, ChangeCommitDTO, GitOid


def _make_change_set(run_id: str = "run_coder") -> ChangeSetDTO:
    return ChangeSetDTO(
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


def test_reviewer_requires_immutable_change_set_evidence():
    # Contract-level: the reviewer's required input is the change-set evidence,
    # not raw code/chat.
    from crewspace.dto.handoffs import STAGE_CONTRACTS

    assert ArtifactType.CHANGE_SET in STAGE_CONTRACTS["reviewer"].required_inputs
    assert ArtifactType.CHANGE_SET in STAGE_CONTRACTS["coder"].produces


def test_change_set_evidence_is_frozen_and_forbids_extra():
    cs = _make_change_set()
    ev = ChangeSetEvidence(producer_run_id="run_coder", change_set=cs, captured_at="2026-08-25T00:00:00Z")
    assert ev.producer_run_id == "run_coder"
    # frozen: mutation attempt raises
    with pytest.raises(ValidationError):
        ev.producer_run_id = "run_hacked"
    # extra fields rejected
    with pytest.raises(ValidationError):
        ChangeSetEvidence(producer_run_id="r", change_set=cs, captured_at="t", surprise="x")


def test_evidence_attached_only_while_producer_running():
    p = DeliveryPipeline()
    p.begin_stage("planner")
    p.complete_stage("planner", produced=["plan", "task_spec"])

    cs = _make_change_set()
    # Before coder begins, attaching evidence is illegal (no running producer).
    with pytest.raises(IllegalPipelineTransition):
        p.attach_artifact("coder", "change_set", ChangeSetEvidence(
            producer_run_id="run_coder", change_set=cs, captured_at="t"))

    p.begin_stage("coder")
    p.attach_artifact("coder", "change_set", ChangeSetEvidence(
        producer_run_id="run_coder", change_set=cs, captured_at="t"))
    assert "change_set" in p.evidence
    assert p.evidence["change_set"].producer_run_id == "run_coder"


def test_reviewer_cannot_start_before_change_set_evidence():
    p = DeliveryPipeline()
    p.begin_stage("planner")
    p.complete_stage("planner", produced=["plan", "task_spec"])
    p.begin_stage("coder")
    p.complete_stage("coder", produced=["code"])  # produced code but NOT the evidence artifact
    # Reviewer still not eligible: change_set evidence artifact is missing.
    assert p.eligible_stage() != "reviewer"
    with pytest.raises(IllegalPipelineTransition):
        p.begin_stage("reviewer")


def test_evidence_is_frozen_after_producer_completes():
    p = DeliveryPipeline()
    p.begin_stage("planner")
    p.complete_stage("planner", produced=["plan", "task_spec"])
    p.begin_stage("coder")
    cs = _make_change_set()
    p.attach_artifact("coder", "change_set", ChangeSetEvidence(
        producer_run_id="run_coder", change_set=cs, captured_at="t"))
    p.complete_stage("coder", produced=["code", "change_set"])
    # Once complete, the producer cannot re-attach (tamper-evident).
    with pytest.raises(IllegalPipelineTransition):
        p.attach_artifact("coder", "change_set", ChangeSetEvidence(
            producer_run_id="run_hacked", change_set=_make_change_set("run_hacked"), captured_at="t2"))
    # Reviewer now eligible on the immutable evidence.
    assert p.eligible_stage() == "reviewer"
    p.begin_stage("reviewer")
    assert p.stage_status["reviewer"] == StageStatus.RUNNING
