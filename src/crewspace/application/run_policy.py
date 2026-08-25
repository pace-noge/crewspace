"""M6.5 slice 1 — Run-scoped default-deny policy + approval checkpoint.

Pure policy primitive. A `RunPolicy` maps a consequential action class to a
required approval decision and defaults to DENY for anything unspecified. The
checkpoint (`evaluate_action`) is fail-closed: an action only proceeds when the
policy resolves to GRANT; `requested`/`denied`/`expired` block execution and
emit a canonical `approval` `EventEnvelope` for auditability (it surfaces in the
M6.4 activity stream / audit export without new UI plumbing).

Action classes mirror the consequential operations tracked across the app
(git push/PR, deploy, package install, network egress, shell command, file
write). The policy deliberately does NOT invent a parallel authorization path —
it composes with the existing default-deny agent-tool / MCP governance.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Literal

from ..dto.events import EventEnvelope, build_event


class ActionDecision(str, Enum):
    """Resolved decision for a consequential action within a run."""

    GRANT = "granted"
    DENY = "denied"
    REQUEST = "requested"


# Consequential action classes the run policy governs. Unknown/unspecified
# classes are denied by default (fail-closed).
KNOWN_ACTION_CLASSES: frozenset[str] = frozenset(
    {
        "git_push",
        "deploy",
        "package_install",
        "network_egress",
        "shell_command",
        "file_write",
    }
)


@dataclass(frozen=True)
class CheckpointResult:
    """Outcome of evaluating a consequential action against the run policy."""

    allowed: bool
    decision: ActionDecision
    event: EventEnvelope


class RunPolicy:
    """Run-scoped default-deny policy for consequential action classes.

    `allowed` lists the action classes that are pre-approved for this run
    (e.g. granted via an earlier approval decision). Anything not in `allowed`
    is denied by default, and the checkpoint emits a `requested` approval event
    so a human (or policy) can resolve it.
    """

    def __init__(self, allowed: set[str] | None = None) -> None:
        self._allowed = frozenset(allowed or set())

    @staticmethod
    def known_action_classes() -> frozenset[str]:
        return KNOWN_ACTION_CLASSES

    def is_consequential(self, action_class: str) -> bool:
        return action_class in KNOWN_ACTION_CLASSES

    def resolve(
        self, action_class: str, *, approved_for: set[str] | None = None
    ) -> tuple[ActionDecision, bool]:
        """Return (decision, is_consequential).

        Fail-closed: an unspecified/unknown action class is DENY. A class that
        is allowed by the run policy (or explicitly approved for this run) is
        GRANT.
        """
        approved_for = approved_for or set()
        if action_class not in KNOWN_ACTION_CLASSES:
            return ActionDecision.DENY, False
        if action_class in self._allowed or action_class in approved_for:
            return ActionDecision.GRANT, True
        return ActionDecision.DENY, True


def evaluate_action(
    policy: RunPolicy,
    action_class: str,
    run_id: str,
    principal_id: str | None = None,
    *,
    approved_for: set[str] | None = None,
    prior_decision: Literal["granted", "denied", "expired", "requested"] | None = None,
) -> CheckpointResult:
    """Evaluate a consequential action against the run policy.

    Fail-closed: only an explicit GRANT (policy allows it, or a prior `granted`
    decision is supplied) lets the action proceed. `requested`/`denied`/
    `expired` block execution. A canonical `approval` `EventEnvelope` is returned
    for every outcome so the decision is auditable.
    """
    approved_for = approved_for or set()

    # A prior granted decision (e.g. from a human approval checkpoint) unlocks
    # the action if the class is consequential; anything else stays blocked.
    if prior_decision == "granted" and policy.is_consequential(action_class):
        decision = ActionDecision.GRANT
    else:
        decision, _ = policy.resolve(action_class, approved_for=approved_for)
        # Surface a pending/unresolved state as a `requested` approval event
        # rather than a flat deny, so the operator can resolve it.
        if decision is ActionDecision.DENY and policy.is_consequential(action_class):
            decision = ActionDecision.REQUEST

    allowed = decision is ActionDecision.GRANT
    event = build_event(
        "approval",
        occurred_at=_now(),
        run_id=run_id,
        payload={
            "decision": decision.value,
            "action_class": action_class,
            "scope": run_id,
            "principal_id": principal_id,
        },
    )
    return CheckpointResult(allowed=allowed, decision=decision, event=event)


def _now():
    from datetime import datetime, timezone

    return datetime.now(timezone.utc)
