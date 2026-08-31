"""M8.4 — Pipeline-participant reference example acceptance tests."""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "examples"))

from pipeline_participant import (
    Coder,
    HumanApprover,
    Planner,
    Reviewer,
    Tester,
    run_delivery_pipeline,
)
from crewspace.application.pipeline import (
    DeliveryPipeline,
    IllegalPipelineTransition,
    PipelineStatus,
    RetryPolicy,
    StageStatus,
)
from crewspace.dto.change_sets import (
    ChangeCommitDTO,
    ChangedFileDTO,
    ChangeSetDTO,
    VerificationResultDTO,
)
from crewspace.dto.handoffs import ArtifactType, ChangeSetEvidence, STAGE_CONTRACTS


def _git(path: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(path), *args],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _real_change_set(tmp_path: Path) -> ChangeSetDTO:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.name", "Pipeline POC")
    _git(repo, "config", "user.email", "pipeline@example.test")
    (repo / "feature.py").write_text("def ready(): return True\n")
    _git(repo, "add", "feature.py")
    _git(repo, "commit", "-m", "add feature")
    head = _git(repo, "rev-parse", "HEAD")
    return ChangeSetDTO(
        repository_id="repo_poc",
        run_id="run_coder",
        branch="crewspace/run_coder",
        base_commit=head,
        head_commit=head,
        commits=(ChangeCommitDTO(sha=head, subject="add feature"),),
        files=(
            ChangedFileDTO(
                path="feature.py", status="added", additions=1, deletions=0
            ),
        ),
        additions=1,
        deletions=0,
        verification=(
            VerificationResultDTO(
                name="pytest", status="passed", summary="green"
            ),
        ),
        artifacts=(),
    )


def test_distinct_roles_drive_versioned_contracts(tmp_path: Path):
    result = run_delivery_pipeline(
        task_description="Ship a verified feature",
        change_set=_real_change_set(tmp_path),
        reviewer_notes="Looks good",
        verification_summary="All tests pass",
    )
    pipeline = result["pipeline"]

    assert pipeline.status == PipelineStatus.SUCCEEDED
    assert result["owners"] == {
        "planner": "agent_planner",
        "coder": "agent_coder",
        "reviewer": "agent_reviewer",
        "tester": "agent_tester",
        "human_approval": "u_bilal",
    }
    # Artifacts flow through versioned HandoffContract types.
    assert all(c.schema_version == "1.0" for c in STAGE_CONTRACTS.values())
    assert pipeline.produced == {
        "plan", "task_spec", "code", "change_set",
        "review", "verification", "delivery_decision",
    }


def test_reviewer_receives_immutable_change_set_evidence(tmp_path: Path):
    result = run_delivery_pipeline(
        task_description="Review this change",
        change_set=_real_change_set(tmp_path),
    )
    evidence = result["evidence"]
    assert isinstance(evidence, ChangeSetEvidence)
    assert evidence.producer_run_id == "run_coder"
    assert evidence.change_set.files[0].path == "feature.py"
    with pytest.raises(Exception):
        evidence.producer_run_id = "tampered"


def test_input_gated_stage_eligibility(tmp_path: Path):
    pipeline = DeliveryPipeline()
    assert pipeline.eligible_stage() == "planner"
    with pytest.raises(IllegalPipelineTransition):
        Coder().run(pipeline, change_set=_real_change_set(tmp_path))

    Planner().run(pipeline, task_description="Plan it")
    assert pipeline.eligible_stage() == "coder"
    with pytest.raises(IllegalPipelineTransition):
        Reviewer().run(pipeline)


def test_failed_stage_cannot_silently_advance_or_duplicate(tmp_path: Path):
    pipeline = DeliveryPipeline(retry_policy=RetryPolicy(max_attempts=1))
    Planner().run(pipeline, task_description="Plan it")
    pipeline.begin_stage("coder")
    pipeline.fail_stage("coder")

    assert pipeline.status == PipelineStatus.FAILED
    with pytest.raises(IllegalPipelineTransition):
        Reviewer().run(pipeline)
    with pytest.raises(IllegalPipelineTransition):
        pipeline.complete_stage("coder", produced=["code", "change_set"])


def test_retries_are_capped_and_stale_outputs_do_not_advance(tmp_path: Path):
    pipeline = DeliveryPipeline(retry_policy=RetryPolicy(max_attempts=2))
    Planner().run(pipeline, task_description="Plan it")

    pipeline.begin_stage("coder")
    pipeline.fail_stage("coder")
    assert pipeline.can_retry("coder")
    pipeline.retry_stage("coder")

    pipeline.begin_stage("coder")
    pipeline.fail_stage("coder")
    assert pipeline.status == PipelineStatus.FAILED
    assert not pipeline.can_retry("coder")
    with pytest.raises(IllegalPipelineTransition):
        pipeline.retry_stage("coder")


def test_human_approval_is_no_auto_advance_gate(tmp_path: Path):
    cs = _real_change_set(tmp_path)
    pipeline = DeliveryPipeline()
    Planner().run(pipeline, task_description="Plan")
    Coder().run(pipeline, change_set=cs)
    Reviewer().run(pipeline)
    Tester().run(pipeline)

    assert pipeline.eligible_stage() == "human_approval"
    pipeline.begin_stage("human_approval")
    with pytest.raises(IllegalPipelineTransition, match="human approval not granted"):
        pipeline.complete_stage(
            "human_approval", produced=[ArtifactType.DELIVERY_DECISION.value]
        )
    assert pipeline.status == PipelineStatus.IN_PROGRESS

    pipeline.grant_human_approval(principal_id="u_bilal", approved=True)
    pipeline.complete_stage(
        "human_approval", produced=[ArtifactType.DELIVERY_DECISION.value]
    )
    assert pipeline.status == PipelineStatus.SUCCEEDED


def test_cancelled_pipeline_blocks_all_downstream_work(tmp_path: Path):
    pipeline = DeliveryPipeline()
    Planner().run(pipeline, task_description="Plan")
    pipeline.cancel()
    assert pipeline.status == PipelineStatus.CANCELLED
    with pytest.raises(IllegalPipelineTransition):
        Coder().run(pipeline, change_set=_real_change_set(tmp_path))


def test_denied_human_approval_cannot_be_overridden(tmp_path: Path):
    cs = _real_change_set(tmp_path)
    pipeline = DeliveryPipeline()
    Planner().run(pipeline, task_description="Plan")
    Coder().run(pipeline, change_set=cs)
    Reviewer().run(pipeline)
    Tester().run(pipeline)
    pipeline.begin_stage("human_approval")
    pipeline.set_prior_approval_decision("denied")
    pipeline.grant_human_approval(principal_id="u_bilal", approved=True)
    with pytest.raises(IllegalPipelineTransition):
        pipeline.complete_stage(
            "human_approval", produced=[ArtifactType.DELIVERY_DECISION.value]
        )
    assert pipeline.status == PipelineStatus.IN_PROGRESS


def test_e2e_real_repo_reaches_verified_change_set_after_human_approval(tmp_path: Path):
    change_set = _real_change_set(tmp_path)
    result = run_delivery_pipeline(
        task_description="Deliver the real change",
        change_set=change_set,
        reviewer_approved=True,
        human_approved=True,
        verification_status="passed",
    )
    pipeline = result["pipeline"]
    evidence = result["evidence"]
    assert pipeline.status == PipelineStatus.SUCCEEDED
    assert evidence.change_set.head_commit == change_set.head_commit
    assert evidence.change_set.verification[0].status == "passed"
    assert pipeline.delivery_decision == "approved"
