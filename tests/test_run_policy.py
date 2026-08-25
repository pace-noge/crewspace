"""M6.5 slice 1 — Run-scoped default-deny policy + approval checkpoint.

Acceptance items covered (first slices of M6.5):
  1. Consequential action classes and the default-deny run policy are defined.
  2. A checkpoint that needs approval emits a canonical `approval` (requested)
     event before the action runs.
  3. `granted` approvals allow the action; `denied`/`expired`/`requested`
     (unresolved) fail closed and block execution.
  4. Approval decision is tied to principal, run, and action class.

The policy is a pure primitive: it maps an action class to a required decision
and defaults to DENY for anything unspecified. The checkpoint produces a
canonical `EventEnvelope` (event_type=approval) so the decision is auditable and
surfaces in the M6.4 activity stream / audit export without new UI plumbing.
"""
from __future__ import annotations

import pytest

from crewspace.application.run_policy import (
    ActionDecision,
    RunPolicy,
    evaluate_action,
)
from crewspace.dto.events import EventEnvelope


CONSEQUENTIAL = {
    "git_push", "deploy", "package_install", "network_egress",
    "shell_command", "file_write",
}


def test_consequential_action_classes_are_defined():
    # The policy knows the consequential classes up front.
    assert CONSEQUENTIAL.issubset(RunPolicy.known_action_classes())


def test_default_deny_blocks_unspecified_action_class():
    policy = RunPolicy()  # empty = pure default-deny
    # An unspecified action class is not allowed.
    decision, _ = policy.resolve("git_push", approved_for=set())
    assert decision is ActionDecision.DENY
    # An unknown/never-defined class also defaults to deny.
    decision2, _ = policy.resolve("some_future_action", approved_for=set())
    assert decision2 is ActionDecision.DENY


def test_allowlisted_action_class_is_granted():
    policy = RunPolicy(allowed={"git_push"})
    decision, _ = policy.resolve("git_push", approved_for={"git_push"})
    assert decision is ActionDecision.GRANT


def test_checkpoint_emits_canonical_approval_requested_event():
    policy = RunPolicy(allowed=set())  # git_push not allowed -> needs approval
    result = evaluate_action(
        policy=policy, action_class="git_push", run_id="run_1",
        principal_id="user_bilal", approved_for=set(),
    )
    # Unresolved -> the action is blocked and a `requested` approval event is emitted.
    assert result.allowed is False
    env = result.event
    assert isinstance(env, EventEnvelope)
    assert env.event_type == "approval"
    assert env.payload.decision == "requested"
    assert env.payload.action_class == "git_push"
    assert env.payload.scope == "run_1"
    assert env.payload.principal_id == "user_bilal"


def test_checkpoint_emits_canonical_approval_granted_event_when_allowed():
    policy = RunPolicy(allowed={"git_push"})
    result = evaluate_action(
        policy=policy, action_class="git_push", run_id="run_1",
        principal_id="user_bilal", approved_for={"git_push"},
    )
    assert result.allowed is True
    assert result.event.payload.decision == "granted"
    assert result.event.payload.action_class == "git_push"


def test_checkpoint_fails_closed_on_denied_expired_or_unresolved():
    policy = RunPolicy(allowed=set())
    # denied
    r1 = evaluate_action(policy, "git_push", "run_1", "u", approved_for=set(),
                         prior_decision="denied")
    assert r1.allowed is False and r1.event.payload.decision == "requested"
    # expired
    r2 = evaluate_action(policy, "git_push", "run_1", "u", approved_for=set(),
                         prior_decision="expired")
    assert r2.allowed is False
    # still requested (unresolved) -> blocked
    r3 = evaluate_action(policy, "git_push", "run_1", "u", approved_for=set(),
                         prior_decision="requested")
    assert r3.allowed is False
    # granted (prior decision honored) -> allowed
    r4 = evaluate_action(policy, "git_push", "run_1", "u", approved_for={"git_push"},
                         prior_decision="granted")
    assert r4.allowed is True
