"""M8.4 — Pipeline-participant reference example.

Demonstrates the M6.6 multi-agent delivery pipeline with distinct agent roles:
planner, coder, reviewer, and tester, each producing versioned `HandoffContract`
artifacts (not free chat). The reviewer receives independent, tamper-evident
`ChangeSetEvidence` from the coder. Human approval is required before final
delivery (NO-AUTO-ADVANCE gate).

This module is a reference orchestration: each role is a callable that drives
`DeliveryPipeline` transitions and artifact attachments, proving the typed
handoff flow without reinventing the state machine.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from crewspace.application.pipeline import DeliveryPipeline
from crewspace.dto.handoffs import ArtifactType, ChangeSetEvidence


# ---------------------------------------------------------------------------
# Roles — each models a distinct pipeline participant.
# ---------------------------------------------------------------------------


class Planner:
    """Plans the task: produces a structured plan and task specification."""

    name = "planner"

    def run(
        self,
        pipeline: DeliveryPipeline,
        *,
        task_description: str,
        steps: List[str] | None = None,
    ) -> Dict[str, Any]:
        pipeline.begin_stage(self.name)
        steps = steps or ["analyze", "implement", "verify"]
        plan = {"task": task_description, "steps": steps}
        pipeline.complete_stage(
            self.name,
            produced=[ArtifactType.PLAN.value, ArtifactType.TASK_SPEC.value],
        )
        return plan


class Coder:
    """Implements the plan: produces code and captures a change-set."""

    name = "coder"

    def run(
        self,
        pipeline: DeliveryPipeline,
        *,
        change_set: Any,
        captured_at: str = "",
    ) -> ChangeSetEvidence:
        pipeline.begin_stage(self.name)
        evidence = ChangeSetEvidence(
            producer_run_id=change_set.run_id if hasattr(change_set, "run_id") else "run_coder",
            change_set=change_set,
            captured_at=captured_at,
        )
        pipeline.attach_artifact(self.name, ArtifactType.CHANGE_SET.value, evidence)
        pipeline.complete_stage(
            self.name,
            produced=[ArtifactType.CODE.value, ArtifactType.CHANGE_SET.value],
        )
        return evidence


class Reviewer:
    """Reviews the change-set: consumes independent, immutable evidence."""

    name = "reviewer"

    def run(
        self,
        pipeline: DeliveryPipeline,
        *,
        approved: bool = True,
        notes: str = "",
    ) -> Dict[str, Any]:
        pipeline.begin_stage(self.name)
        evidence = pipeline.evidence.get(ArtifactType.CHANGE_SET.value)
        if evidence is not None:
            assert isinstance(evidence, ChangeSetEvidence)
        review = {"approved": approved, "notes": notes}
        pipeline.complete_stage(self.name, produced=[ArtifactType.REVIEW.value])
        return review


class Tester:
    """Verifies the change: produces a verification result."""

    name = "tester"

    def run(
        self,
        pipeline: DeliveryPipeline,
        *,
        status: str = "passed",
        summary: str = "",
    ) -> Dict[str, Any]:
        pipeline.begin_stage(self.name)
        verification = {"status": status, "summary": summary}
        pipeline.complete_stage(
            self.name,
            produced=[ArtifactType.VERIFICATION.value],
        )
        return verification


class HumanApprover:
    """Human approval gate: NO-AUTO-Advance — explicit grant required."""

    def approve(
        self,
        pipeline: DeliveryPipeline,
        *,
        principal_id: str,
        approved: bool = True,
    ) -> None:
        pipeline.begin_stage("human_approval")
        pipeline.grant_human_approval(principal_id=principal_id, approved=approved)
        pipeline.complete_stage(
            "human_approval",
            produced=[ArtifactType.DELIVERY_DECISION.value],
        )


# ---------------------------------------------------------------------------
# Orchestrator — drives the full pipeline flow end-to-end.
# ---------------------------------------------------------------------------


def run_delivery_pipeline(
    *,
    task_description: str,
    change_set: Any,
    reviewer_approved: bool = True,
    reviewer_notes: str = "",
    human_principal_id: str = "u_bilal",
    human_approved: bool = True,
    verification_status: str = "passed",
    verification_summary: str = "",
) -> Dict[str, Any]:
    """Drive a full planner -> coder -> reviewer -> tester -> human_approval
    pipeline and return all role outputs for inspection."""
    pipeline = DeliveryPipeline()
    owners = {
        "planner": "agent_planner",
        "coder": "agent_coder",
        "reviewer": "agent_reviewer",
        "tester": "agent_tester",
        "human_approval": human_principal_id,
    }

    plan = Planner().run(pipeline, task_description=task_description)
    evidence = Coder().run(pipeline, change_set=change_set)
    review = Reviewer().run(
        pipeline, approved=reviewer_approved, notes=reviewer_notes,
    )
    verification = Tester().run(
        pipeline, status=verification_status, summary=verification_summary,
    )
    if human_approved:
        HumanApprover().approve(
            pipeline,
            principal_id=human_principal_id,
            approved=human_approved,
        )

    return {
        "pipeline": pipeline,
        "owners": owners,
        "plan": plan,
        "evidence": evidence,
        "review": review,
        "verification": verification,
    }
