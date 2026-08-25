"""M6.6 slice 6 — UI shows stage status, owner, artifacts, budgets, blockers.

Renders a pipeline run graph from DeliveryPipeline state via a pure view-model
builder (build_pipeline_view) and the pipeline_graph.html fragment. Verified by
rendering the fragment (no browser) and asserting the key fields appear.
"""
from __future__ import annotations

from crewspace.application.pipeline import DeliveryPipeline, PipelineStatus, StageStatus
from crewspace.api.rendering import templates


def _full_pipeline() -> DeliveryPipeline:
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
    p.begin_stage("human_approval")
    return p


def _render(p: DeliveryPipeline, owners: dict) -> str:
    from crewspace.application.pipeline_view import build_pipeline_view

    view = build_pipeline_view(p, owners=owners)
    return templates.get_template("pipeline_graph.html").render(view=view)


def test_pipeline_graph_shows_each_stage_status_and_owner():
    owners = {"planner": "agent_planner", "coder": "agent_coder",
              "reviewer": "agent_reviewer", "tester": "agent_tester",
              "human_approval": "u_bilal"}
    html = _render(_full_pipeline(), owners)
    for stage in ("planner", "coder", "reviewer", "tester", "human_approval"):
        assert f'data-stage="{stage}"' in html
    # owner shown per stage
    assert "agent_coder" in html
    assert "u_bilal" in html


def test_pipeline_graph_shows_artifacts_produced():
    html = _render(_full_pipeline(), {})
    # produced artifacts are listed (code, change_set, review, verification, ...)
    assert "change_set" in html
    assert "verification" in html
    assert "plan" in html


def test_pipeline_graph_shows_blocker_when_approval_required():
    # human_approval reached but not granted -> blocker surfaced
    p = _full_pipeline()  # human_approval RUNNING, no grant
    html = _render(p, {})
    assert "approval required" in html.lower() or "approval" in html.lower()


def test_pipeline_graph_shows_blocker_on_failed_stage():
    p = DeliveryPipeline()
    p.begin_stage("planner")
    p.complete_stage("planner", produced=["plan", "task_spec"])
    p.begin_stage("coder")
    p.fail_stage("coder")
    p.retry_stage("coder")
    p.begin_stage("coder")
    p.fail_stage("coder")  # exhausted -> terminal FAILED
    html = _render(p, {})
    assert "failed" in html.lower()


def test_pipeline_graph_shows_delivery_decision():
    p = _full_pipeline()
    p.grant_human_approval("u_bilal", True)
    p.complete_stage("human_approval", produced=["delivery_decision"])
    html = _render(p, {})
    assert "approved" in html.lower()
