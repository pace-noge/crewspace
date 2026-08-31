"""M8.3 — Approval-aware reference agent path.

A reference remote coding agent drives the M6.5 run-scoped approval policy
end to end. At any consequential action class the agent delegates to an
`ApprovalGate`, which reuses the app's canonical `evaluate_action` seam: it
emits an `approval` `EventEnvelope` (requested) and pauses before any side
effect; it resumes only on an explicit `granted` bound to that run + action
class; and it blocks fail-closed on `denied`/`expired`/`requested`. A grant
for one class never unlocks another, and replay of a non-granted decision
cannot execute.

The gate is transport-agnostic: a caller supplies how a decision is awaited
(via `grant(...)`), so it can be bridged to any decision channel (inbox,
device prompt, WS frame) later without changing policy semantics. Policy
semantics are reused verbatim from `application.run_policy` —
`ApprovalGate` never reinvents them.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from crewspace.application.run_policy import (
    ActionDecision,
    RunPolicy,
    evaluate_action,
)
from crewspace.dto.events import EventEnvelope


@dataclass(frozen=True)
class ApprovalPaused(Exception):
    """Raised when a consequential action is paused awaiting a decision.

    Carries the canonical `approval` event emitted by the checkpoint so the
    reference agent can surface it (activity stream, audit export, or any
    decision channel).
    """

    run_id: str
    action_class: str
    decision: ActionDecision
    event: EventEnvelope


@dataclass(frozen=True)
class ApprovalGrant:
    """A human (or policy) decision bound to a specific run + action class."""

    run_id: str
    action_class: str
    decision: str = "granted"  # granted | denied | expired | requested


class ApprovalGate:
    """Run-scoped approval gate for a reference agent's consequential actions.

    Deterministic and fail-closed:
    - `perform(action_class, execute)` evaluates the run policy via the app's
      `evaluate_action`; without an outstanding class-bound grant it emits a
      canonical `approval` event and raises `ApprovalPaused` BEFORE executing.
    - `grant(ApprovalGrant)` records a terminal decision. Only an explicit
      `granted` bound to (run_id, action_class) unlocks the action (once).
    - `denied`/`expired`/`requested` bind terminally: the action can never run,
      even against a later different grant for the same class (replay fails
      closed).
    - A grant for one class never unlocks a different class (scope-escalation
      guard, matching `evaluate_action` semantics).
    """

    def __init__(
        self,
        *,
        policy: RunPolicy,
        run_id: str,
        event_recorder: Callable[[EventEnvelope], None] | None = None,
        principal_id: str | None = None,
    ) -> None:
        self._policy = policy
        self._run_id = run_id
        self._principal_id = principal_id
        self._event_recorder = event_recorder
        # (run_id, action_class) -> terminal decision already recorded.
        self._outcomes: dict[tuple[str, str], str] = {}

    # -- decision surface ---------------------------------------------------
    def grant(self, grant: ApprovalGrant) -> bool:
        """Record a terminal decision for a (run, class). Returns True if a
        prior terminal outcome is being overridden by a later GRANT, False if
        the action stays blocked (non-granted decision, or already terminal)."""
        key = (grant.run_id, grant.action_class)
        # Fail-closed: once a decision has been recorded, only a *different*
        # class/run can move; a non-granted decision is terminal and cannot be
        # overridden by any later grant for the same class.
        if grant.decision != "granted":
            self._outcomes[key] = grant.decision
            return False
        prior = self._outcomes.get(key)
        if prior is not None and prior != "granted":
            # A previously denied/expired/requested decision is terminal:
            # a later grant must NOT resurrect it.
            return False
        self._outcomes[key] = "granted"
        return True

    # -- execution surface --------------------------------------------------
    def perform(self, action_class: str, execute: Callable[[], object]) -> object:
        """Attempt a consequential action.

        Reuses `evaluate_action` (the app seam) exactly: only an explicit
        granted (policy-allowed OR prior class-bound grant) executes; anything
        else emits the canonical `approval` event and raises
        `ApprovalPaused` before any side effect.
        """
        checkpoint = evaluate_action(
            self._policy,
            action_class,
            self._run_id,
            principal_id=self._principal_id,
            approved_for=(
                {action_class}
                if self._outcomes.get((self._run_id, action_class)) == "granted"
                else set()
            ),
            prior_decision=self._outcomes.get((self._run_id, action_class)),
        )
        if self._event_recorder is not None:
            self._event_recorder(checkpoint.event)
        if not checkpoint.allowed:
            raise ApprovalPaused(
                run_id=self._run_id,
                action_class=action_class,
                decision=checkpoint.decision,
                event=checkpoint.event,
            )
        return execute()
