"""M6.6 — Versioned handoff contracts for the multi-agent delivery pipeline.

Pure DTO layer (no sqlalchemy / websocket / infrastructure imports) so the
contracts stay migration-safe and unit-testable, mirroring the M6.4 event
envelope discipline.

Every delivery stage declares the artifacts it REQUIRES as input and the
artifacts it PRODUCES, so handoffs are structured (not unconstrained
agent-to-agent chat). Contracts are versioned and reject unknown fields.
"""
from __future__ import annotations

from enum import Enum
from typing import Dict, FrozenSet

from pydantic import BaseModel, ConfigDict, Field

from crewspace.dto.change_sets import ChangeSetDTO


class ArtifactType(str, Enum):
    """Structured artifacts passed between pipeline stages."""

    PLAN = "plan"
    CODE = "code"
    REVIEW = "review"
    VERIFICATION = "verification"
    DELIVERY_DECISION = "delivery_decision"
    TASK_SPEC = "task_spec"
    CHANGE_SET = "change_set"


class StageName(str, Enum):
    PLANNER = "planner"
    CODER = "coder"
    REVIEWER = "reviewer"
    TESTER = "tester"
    HUMAN_APPROVAL = "human_approval"


class HandoffContract(BaseModel):
    """Versioned contract for one pipeline stage.

    `required_inputs` are artifacts the stage needs from upstream stages;
    `produces` are artifacts it emits for downstream stages.
    """

    model_config = ConfigDict(extra="forbid")

    schema_version: str = Field(default="1.0", pattern=r"^[0-9]+\.[0-9]+$")
    required_inputs: FrozenSet[str] = Field(default_factory=frozenset)
    produces: FrozenSet[str] = Field(default_factory=frozenset)


class ChangeSetEvidence(BaseModel):
    """Immutable, tamper-evident change-set handed from coder to reviewer.

    Wraps the coder's frozen ChangeSetDTO (captured at terminal run state per
    M6.3) plus the producing run id and capture time. Frozen + extra=forbid so
    the reviewer receives evidence it cannot mutate (independent context).
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    producer_run_id: str
    change_set: ChangeSetDTO
    captured_at: str


# Ordered stage graph: planner -> coder -> reviewer -> tester -> human_approval.
STAGE_CONTRACTS: Dict[str, HandoffContract] = {
    StageName.PLANNER.value: HandoffContract(
        schema_version="1.0",
        required_inputs=frozenset(),
        produces=frozenset({ArtifactType.PLAN.value, ArtifactType.TASK_SPEC.value}),
    ),
    StageName.CODER.value: HandoffContract(
        schema_version="1.0",
        required_inputs=frozenset({ArtifactType.PLAN.value}),
        produces=frozenset({ArtifactType.CODE.value, ArtifactType.CHANGE_SET.value}),
    ),
    StageName.REVIEWER.value: HandoffContract(
        schema_version="1.0",
        required_inputs=frozenset({ArtifactType.CHANGE_SET.value}),
        produces=frozenset({ArtifactType.REVIEW.value}),
    ),
    StageName.TESTER.value: HandoffContract(
        schema_version="1.0",
        required_inputs=frozenset({ArtifactType.CODE.value, ArtifactType.REVIEW.value}),
        produces=frozenset({ArtifactType.VERIFICATION.value}),
    ),
    StageName.HUMAN_APPROVAL.value: HandoffContract(
        schema_version="1.0",
        required_inputs=frozenset({ArtifactType.VERIFICATION.value}),
        produces=frozenset({ArtifactType.DELIVERY_DECISION.value}),
    ),
}


def validate_pipeline_graph(
    contracts: Dict[str, HandoffContract] | None = None,
) -> bool:
    """Return True iff every stage's required inputs are produced by a prior
    stage in the ordered graph (no silent handoff gaps)."""
    graph = contracts if contracts is not None else STAGE_CONTRACTS
    produced_so_far: set[str] = set()
    for stage, contract in graph.items():
        missing = set(contract.required_inputs) - produced_so_far
        if missing:
            return False
        produced_so_far |= set(contract.produces)
    return True
