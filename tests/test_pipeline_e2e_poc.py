"""M6.6 slice 7 (final) — End-to-end real-repo POC reaching a verified
human-approved change set (acceptance item 7).

Drives a real git temp repo -> real ChangeSetDTO (built from the repo's actual
commit SHA + a real relative file path) -> the full DeliveryPipeline
(planner -> coder -> reviewer -> tester -> human_approval) -> a human approval,
and asserts the pipeline terminates in a verified, immutable, human-approved
change set whose head_commit matches the real repo HEAD.
"""
from __future__ import annotations

import subprocess
import tempfile
from pathlib import Path

from crewspace.application.pipeline import DeliveryPipeline
from crewspace.application.pipeline_view import build_pipeline_view
from crewspace.dto.handoffs import ChangeSetEvidence
from crewspace.dto.change_sets import (
    ChangeSetDTO,
    ChangeCommitDTO,
    ChangedFileDTO,
    VerificationResultDTO,
)
from crewspace.dto.handoffs import ArtifactType


def _make_real_repo() -> tuple[Path, str]:
    """Create a real temp git repo with one commit; return (path, head_sha)."""
    repo = Path(tempfile.mkdtemp(prefix="crewspace-poc-"))
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "poc@crewspace"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "poc"], cwd=repo, check=True)
    (repo / "hello.py").write_text("print('hello')\n")
    subprocess.run(["git", "add", "hello.py"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "add hello"], cwd=repo, check=True)
    head = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, text=True, check=True
    ).stdout.strip()
    return repo, head


def test_end_to_end_pipeline_reaches_human_approved_change_set():
    repo, head = _make_real_repo()
    # The change set is built from the REAL repo state (validates GitOid / relative path).
    cs = ChangeSetDTO(
        repository_id="repo_poc",
        run_id="run_coder",
        branch="crewspace/run_coc",
        base_commit=head,  # single commit: base == head for this POC
        head_commit=head,
        commits=(ChangeCommitDTO(sha=head, subject="add hello"),),
        files=(ChangedFileDTO(path="hello.py", status="added", additions=1, deletions=0),),
        additions=1,
        deletions=0,
        verification=(VerificationResultDTO(name="pytest", status="passed", summary="green"),),
        artifacts=(),
    )

    pipeline = DeliveryPipeline()
    owners = {"planner": "agent_planner", "coder": "agent_coder",
              "reviewer": "agent_reviewer", "tester": "agent_tester",
              "human_approval": "u_bilal"}

    # planner
    pipeline.begin_stage("planner")
    pipeline.complete_stage("planner", produced=["plan", "task_spec"])
    # coder produces the immutable change-set evidence (real repo capture)
    pipeline.begin_stage("coder")
    evidence = ChangeSetEvidence(producer_run_id="run_coder", change_set=cs, captured_at="2026-08-25T00:00:00Z")
    pipeline.attach_artifact("coder", ArtifactType.CHANGE_SET.value, evidence)
    pipeline.complete_stage("coder", produced=["code", ArtifactType.CHANGE_SET.value])
    # reviewer consumes the immutable evidence (independent context)
    pipeline.begin_stage("reviewer")
    pipeline.complete_stage("reviewer", produced=["review"])
    # tester verifies
    pipeline.begin_stage("tester")
    pipeline.complete_stage("tester", produced=["verification"])
    # human approval required before delivery: the stage may begin (inputs
    # satisfied) but cannot complete without an explicit grant.
    pipeline.begin_stage("human_approval")
    assert pipeline.eligible_stage() == "human_approval"
    from crewspace.application.pipeline import IllegalPipelineTransition

    try:
        pipeline.complete_stage("human_approval", produced=["delivery_decision"])
        raise AssertionError("delivery completed without human approval")
    except IllegalPipelineTransition:
        pass
    pipeline.grant_human_approval("u_bilal", True)
    assert pipeline.delivery_decision == "approved"
    pipeline.complete_stage("human_approval", produced=["delivery_decision"])

    # Terminal success, delivery approved.
    assert pipeline.status.value == "succeeded"
    assert pipeline.delivery_decision == "approved"

    # The captured change set is immutable and linked to the real repo HEAD.
    captured = pipeline.evidence[ArtifactType.CHANGE_SET.value]
    assert isinstance(captured, ChangeSetEvidence)
    assert captured.change_set.head_commit == head  # verified linkage to real repo
    # Frozen: attempting to mutate the change set raises.
    frozen_ok = True
    try:
        captured.change_set.head_commit = "z" * 40
        frozen_ok = False
    except Exception:
        pass
    assert frozen_ok, "captured change set must be immutable"

    # UI view model reflects the finished, approved run.
    view = build_pipeline_view(pipeline, owners=owners)
    assert view.status == "succeeded"
    assert view.delivery_decision == "approved"
    assert all(s.status == "succeeded" for s in view.stages)
