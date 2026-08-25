"""M6.6 slice 1 — Versioned handoff contracts (acceptance item 1).

Each delivery-pipeline stage declares, in a versioned contract, the artifacts
it REQUIRES as input and the artifacts it PRODUCES. Handoffs are structured
artifacts (not unconstrained agent-to-agent chat). The contracts are pure DTOs
(no DB/websocket imports) so they stay migration-safe and unit-testable.
"""
from __future__ import annotations

import pytest

from crewspace.dto.handoffs import (
    ArtifactType,
    HandoffContract,
    STAGE_CONTRACTS,
    validate_pipeline_graph,
)
from pydantic import ValidationError


def test_handoff_contracts_define_required_inputs_and_outputs_per_stage():
    # Every delivery stage has a contract with explicit required inputs/outputs.
    for stage in ("planner", "coder", "reviewer", "tester", "human_approval"):
        assert stage in STAGE_CONTRACTS, f"missing contract for stage {stage}"
        contract = STAGE_CONTRACTS[stage]
        assert isinstance(contract, HandoffContract)
        assert contract.schema_version == "1.0"
        assert isinstance(contract.required_inputs, frozenset)
        assert isinstance(contract.produces, frozenset)


def test_pipeline_stage_order_is_planner_to_human_approval():
    order = list(STAGE_CONTRACTS.keys())
    assert order == ["planner", "coder", "reviewer", "tester", "human_approval"]


def test_handoff_graph_outputs_feed_next_stage_required_inputs():
    # Each stage's required inputs must be producible by some prior stage, and
    # each stage must produce what the next stage requires (no silent gaps).
    produced_so_far: set[str] = set()
    stages = list(STAGE_CONTRACTS.items())
    for idx, (stage, contract) in enumerate(stages):
        if stage == "planner":
            assert contract.required_inputs == frozenset(), "planner needs no upstream artifact"
        else:
            missing = contract.required_inputs - produced_so_far
            assert not missing, f"stage {stage} requires unproduced inputs: {missing}"
        produced_so_far |= set(contract.produces)
    assert ArtifactType.VERIFICATION in STAGE_CONTRACTS["tester"].produces
    assert ArtifactType.VERIFICATION in STAGE_CONTRACTS["human_approval"].required_inputs
    assert ArtifactType.DELIVERY_DECISION in STAGE_CONTRACTS["human_approval"].produces


def test_validate_pipeline_graph_rejects_broken_handoff():
    broken = {
        "planner": HandoffContract(schema_version="1.0", required_inputs=frozenset(), produces=frozenset({"plan"})),
        "coder": HandoffContract(schema_version="1.0", required_inputs=frozenset({"nonexistent_artifact"}), produces=frozenset()),
    }
    assert validate_pipeline_graph(broken) is False


def test_handoff_contract_rejects_unknown_fields():
    with pytest.raises(ValidationError):
        HandoffContract(
            schema_version="1.0",
            required_inputs={"plan"},
            produces={"code"},
            unexpected_field="boom",
        )
