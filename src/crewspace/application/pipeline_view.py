"""M6.6 slice 6 — Pure view-model builder for the pipeline run-graph UI.

Turns a DeliveryPipeline into a serializable view model the templates render.
Kept free of DB/request dependencies so it is unit-testable and reusable from
any router.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional

from crewspace.application.pipeline import DeliveryPipeline
from crewspace.dto.handoffs import STAGE_CONTRACTS


@dataclass
class PipelineStageView:
    name: str
    status: str
    owner: Optional[str]
    required: List[str] = field(default_factory=list)
    produced: List[str] = field(default_factory=list)
    blockers: List[str] = field(default_factory=list)


@dataclass
class PipelineView:
    status: str
    delivery_decision: Optional[str]
    stages: List[PipelineStageView] = field(default_factory=list)
    blockers: List[str] = field(default_factory=list)


_HUMAN_STAGE = "human_approval"


def build_pipeline_view(pipeline: DeliveryPipeline, owners: Optional[Dict[str, str]] = None) -> PipelineView:
    """Build a render-ready view model of the pipeline run graph.

    `owners` maps stage name -> principal/agent id responsible for it (UI ownership).
    """
    owners = owners or {}
    stages: List[PipelineStageView] = []
    blockers: List[str] = []

    for stage in STAGE_CONTRACTS:
        status = pipeline.stage_status[stage].value
        contract = pipeline.contracts[stage]
        produced = sorted(pipeline.produced_by_stage.get(stage, set()))
        required = sorted(contract.required_inputs)
        stage_blockers: List[str] = []

        if status == "failed":
            if pipeline.can_retry(stage):
                stage_blockers.append("failed; retry available")
            else:
                stage_blockers.append("failed; retries exhausted")
        elif status == "running":
            if stage == _HUMAN_STAGE and pipeline.delivery_decision != "approved":
                stage_blockers.append("approval required before delivery")
        elif status == "pending":
            # A pending stage that sits before an incomplete stage is simply queued;
            # only flag it if an upstream failure already blocks the whole pipeline.
            pass

        if stage_blockers:
            blockers.append(f"{stage}: " + "; ".join(stage_blockers))

        stages.append(
            PipelineStageView(
                name=stage,
                status=status,
                owner=owners.get(stage),
                required=required,
                produced=produced,
                blockers=stage_blockers,
            )
        )

    if pipeline.status == "failed":
        blockers.append("pipeline failed; delivery blocked")
    elif pipeline.status == "cancelled":
        blockers.append("pipeline cancelled")

    return PipelineView(
        status=pipeline.status.value,
        delivery_decision=pipeline.delivery_decision,
        stages=stages,
        blockers=[b for b in blockers if b],
    )
