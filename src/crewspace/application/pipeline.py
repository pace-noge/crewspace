"""M6.6 — Deterministic delivery-pipeline state machine (slice 2, item 2).

Builds a linear stage machine over the versioned STAGE_CONTRACTS (slice 1):
planner -> coder -> reviewer -> tester -> human_approval. Each stage only
starts when its required input artifacts are present; retries are capped by a
RetryPolicy; once a stage exhausts its retries the pipeline fails closed to
FAILED and no downstream stage may run; cancelling blocks all transitions.

This module is pure logic (depends only on the handoff DTO), so it is
unit-testable without a DB and stays migration-safe.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Dict, List, Optional

from crewspace.dto.handoffs import STAGE_CONTRACTS, HandoffContract, validate_pipeline_graph


class PipelineStatus(str, Enum):
    IN_PROGRESS = "in_progress"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


class StageStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


class IllegalPipelineTransition(ValueError):
    """Raised when a transition would break pipeline invariants."""


@dataclass(frozen=True)
class RetryPolicy:
    """Caps how many times a single stage may be attempted."""

    max_attempts: int = 3

    def __post_init__(self) -> None:
        if self.max_attempts < 1:
            raise ValueError("max_attempts must be >= 1")


@dataclass
class DeliveryPipeline:
    """Deterministic stage machine over versioned handoff contracts."""

    contracts: Dict[str, HandoffContract] = field(default_factory=lambda: dict(STAGE_CONTRACTS))
    retry_policy: RetryPolicy = field(default_factory=RetryPolicy)

    # runtime state
    status: PipelineStatus = field(default=PipelineStatus.IN_PROGRESS, init=False)
    stage_status: Dict[str, StageStatus] = field(init=False)
    attempts: Dict[str, int] = field(init=False)
    produced: set = field(default_factory=set, init=False)
    evidence: Dict[str, object] = field(default_factory=dict, init=False)

    def __post_init__(self) -> None:
        if not validate_pipeline_graph(self.contracts):
            raise IllegalPipelineTransition("contract graph has an unproducible required input")
        self.stage_status = {s: StageStatus.PENDING for s in self.contracts}
        self.attempts = {s: 0 for s in self.contracts}

    # --- derived helpers -------------------------------------------------

    @property
    def _stage_order(self) -> List[str]:
        return list(self.contracts.keys())

    def _is_terminal(self) -> bool:
        return self.status in (
            PipelineStatus.SUCCEEDED,
            PipelineStatus.FAILED,
            PipelineStatus.CANCELLED,
        )

    def _first_incomplete(self) -> Optional[str]:
        for stage in self._stage_order:
            if self.stage_status[stage] != StageStatus.SUCCEEDED:
                return stage
        return None

    def eligible_stage(self) -> Optional[str]:
        """The next stage that may begin: the first incomplete stage whose
        required inputs are already produced, or None if none/terminal."""
        if self._is_terminal():
            return None
        target = self._first_incomplete()
        if target is None:
            return None
        required = self.contracts[target].required_inputs
        return target if required.issubset(self.produced) else None

    def can_retry(self, stage: str) -> bool:
        if self.stage_status[stage] != StageStatus.FAILED:
            return False
        return self.attempts[stage] < self.retry_policy.max_attempts

    # --- transitions ------------------------------------------------------

    def begin_stage(self, stage: str) -> None:
        if stage not in self.contracts:
            raise IllegalPipelineTransition(f"unknown stage: {stage}")
        if self._is_terminal():
            raise IllegalPipelineTransition(f"pipeline is {self.status.value}; no transitions allowed")
        if self.stage_status[stage] == StageStatus.SUCCEEDED:
            raise IllegalPipelineTransition(f"stage {stage} already succeeded")
        if self.eligible_stage() != stage:
            raise IllegalPipelineTransition(
                f"stage {stage} is not eligible (inputs not satisfied or out of order)"
            )
        self.attempts[stage] += 1
        self.stage_status[stage] = StageStatus.RUNNING

    def complete_stage(self, stage: str, produced: Optional[List[str]] = None) -> None:
        if self._is_terminal():
            raise IllegalPipelineTransition(f"pipeline is {self.status.value}; no transitions allowed")
        if self.stage_status[stage] != StageStatus.RUNNING:
            raise IllegalPipelineTransition(f"stage {stage} is not running")
        self.stage_status[stage] = StageStatus.SUCCEEDED
        for art in produced or []:
            self.produced.add(art)
        if self._first_incomplete() is None:
            self.status = PipelineStatus.SUCCEEDED

    def fail_stage(self, stage: str) -> None:
        if self._is_terminal():
            raise IllegalPipelineTransition(f"pipeline is {self.status.value}; no transitions allowed")
        if self.stage_status[stage] != StageStatus.RUNNING:
            raise IllegalPipelineTransition(f"stage {stage} is not running")
        self.stage_status[stage] = StageStatus.FAILED
        if not self.can_retry(stage):
            self.status = PipelineStatus.FAILED

    def attach_artifact(self, stage: str, name: str, payload: object) -> None:
        """Attach an immutable artifact produced by a running stage (e.g. the
        coder's change-set evidence for the reviewer). Only valid while the
        producer stage is RUNNING; once the stage completes the evidence is
        frozen and cannot be re-attached (tamper-evident)."""
        if self._is_terminal():
            raise IllegalPipelineTransition(f"pipeline is {self.status.value}; no transitions allowed")
        if stage not in self.contracts:
            raise IllegalPipelineTransition(f"unknown stage: {stage}")
        if self.stage_status[stage] != StageStatus.RUNNING:
            raise IllegalPipelineTransition(f"stage {stage} is not running; artifacts attach only while running")
        if name in self.evidence:
            raise IllegalPipelineTransition(f"artifact {name} is already attached and frozen")
        self.evidence[name] = payload

    def retry_stage(self, stage: str) -> None:
        if not self.can_retry(stage):
            raise IllegalPipelineTransition(
                f"stage {stage} cannot be retried (exhausted or not failed)"
            )
        self.stage_status[stage] = StageStatus.PENDING

    def cancel(self) -> None:
        if self._is_terminal():
            raise IllegalPipelineTransition(f"pipeline already {self.status.value}")
        self.status = PipelineStatus.CANCELLED
