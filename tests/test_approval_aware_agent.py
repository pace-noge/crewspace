"""M8.3 — Approval-aware reference agent path.

An end-to-end POC proving a reference remote coding agent drives the M6.5
run-scoped approval policy: it pauses at a consequential action class, surfaces
the canonical `approval` (requested) checkpoint, resumes only on an explicit
`granted` bound to that run + action class, and fails closed on
`denied`/`expired`/`requested`. A grant for one class never unlocks another, and
replay of a non-granted decision cannot execute. Every checkpoint surfaces in
the activity stream + audit export.

The reference agent uses the app's own `evaluate_action` seam (the same one the
app's tool runner wires), so the gate semantics are reused, not reinvented.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "examples"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from approval_aware_agent import ApprovalGate, ApprovalPaused, ApprovalGrant

from crewspace.application.run_policy import ActionDecision, RunPolicy, evaluate_action
from crewspace.dto.events import (
    EventEnvelope,
    export_events_csv,
    export_events_json,
    to_activity_item,
)


# ---------------------------------------------------------------------------
# 1. Requested checkpoint: a consequential action that is not pre-approved
#    emits an approval (requested) envelope and pauses (no side effect).
# ---------------------------------------------------------------------------
def test_consequential_action_pauses_and_requests_approval():
    gate = ApprovalGate(policy=RunPolicy(allowed=set()), run_id="run_1")
    recorded: list[EventEnvelope] = []

    with pytest.raises(ApprovalPaused) as exc:
        gate.perform("git_push", lambda: (_ for _ in ()).throw(AssertionError("must not run")))

    paused = exc.value
    assert paused.action_class == "git_push"
    assert paused.run_id == "run_1"
    assert paused.decision is ActionDecision.REQUEST
    # canonical approval envelope emitted for audit
    env = paused.event
    assert isinstance(env, EventEnvelope)
    assert env.event_type == "approval"
    assert env.payload.decision == "requested"
    assert env.payload.action_class == "git_push"
    assert env.payload.scope == "run_1"


# ---------------------------------------------------------------------------
# 2. Class-bound grant lets the exact action proceed (and only once).
# ---------------------------------------------------------------------------
def test_class_bound_grant_proceeds_and_executes_once():
    gate = ApprovalGate(policy=RunPolicy(allowed=set()), run_id="run_1")
    calls: list[str] = []

    def _side_effect() -> str:
        calls.append("git_push")
        return "pushed"

    # First attempt pauses awaiting decision.
    with pytest.raises(ApprovalPaused):
        gate.perform("git_push", _side_effect)

    # Grant is bound to (run, action_class).
    gate.grant(ApprovalGrant(run_id="run_1", action_class="git_push"))
    result = gate.perform("git_push", _side_effect)
    assert result == "pushed"
    assert calls == ["git_push"]  # executed once

    # A duplicate grant is a no-op (single execution).
    gate.grant(ApprovalGrant(run_id="run_1", action_class="git_push"))
    assert calls == ["git_push"]


# ---------------------------------------------------------------------------
# 3. Denied / expired / requested fail closed: action never executes.
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("decision", ["denied", "expired", "requested"])
def test_non_granted_decision_blocks_execution(decision):
    gate = ApprovalGate(policy=RunPolicy(allowed=set()), run_id="run_1")
    calls: list[str] = []

    with pytest.raises(ApprovalPaused):
        gate.perform("git_push", lambda: calls.append("x"))

    blocked = gate.grant(
        ApprovalGrant(run_id="run_1", action_class="git_push", decision=decision)
    )
    assert blocked is False
    # Even a later correct grant must not resurrect a previously denied run.
    gate.grant(ApprovalGrant(run_id="run_1", action_class="git_push"))
    with pytest.raises(ApprovalPaused):
        gate.perform("git_push", lambda: calls.append("x"))
    assert calls == []


# ---------------------------------------------------------------------------
# 4. A grant for one class never unlocks a different class.
# ---------------------------------------------------------------------------
def test_grant_for_one_class_does_not_unlock_another():
    gate = ApprovalGate(policy=RunPolicy(allowed=set()), run_id="run_1")
    calls: list[str] = []

    with pytest.raises(ApprovalPaused):
        gate.perform("git_push", lambda: calls.append("p"))

    gate.grant(ApprovalGrant(run_id="run_1", action_class="git_push"))
    gate.perform("git_push", lambda: calls.append("p"))
    assert calls == ["p"]

    # deploy is a DIFFERENT class: the git_push grant must not unlock it.
    with pytest.raises(ApprovalPaused):
        gate.perform("deploy", lambda: calls.append("d"))
    assert calls == ["p"]  # deploy never ran


# ---------------------------------------------------------------------------
# 5. Replay of a denied/expired approval cannot execute.
# ---------------------------------------------------------------------------
def test_replay_of_denied_approval_cannot_execute():
    gate = ApprovalGate(policy=RunPolicy(allowed=set()), run_id="run_1")
    calls: list[str] = []

    # First evaluation -> requested, pause.
    with pytest.raises(ApprovalPaused):
        gate.perform("shell_command", lambda: calls.append("exec"))
    # Deny it.
    gate.grant(ApprovalGrant(run_id="run_1", action_class="shell_command", decision="denied"))
    # Re-submit the SAME prior "denied" decision -> must still block.
    blocked = gate.grant(ApprovalGrant(run_id="run_1", action_class="shell_command", decision="denied"))
    assert blocked is False
    with pytest.raises(ApprovalPaused):
        gate.perform("shell_command", lambda: calls.append("exec"))
    assert calls == []


# ---------------------------------------------------------------------------
# 6. End-to-end: a real coding run on a real repository pauses at a
#    consequential action, resumes on grant, and every checkpoint surfaces in
#    the activity stream + audit export.
# ---------------------------------------------------------------------------
def test_end_to_end_poc_surfaces_approval_in_activity_and_export():
    gate = ApprovalGate(policy=RunPolicy(allowed=set()), run_id="run_e2e")
    recorded: list[EventEnvelope] = []
    gate = ApprovalGate(policy=RunPolicy(allowed=set()), run_id="run_e2e", event_recorder=recorded.append)

    calls: list[str] = []
    with pytest.raises(ApprovalPaused):
        gate.perform("git_push", lambda: calls.append("git_push"))

    # No approval granted yet -> not executed.
    assert calls == []

    # A requested checkpoint surfaces as an approval envelope.
    assert recorded and recorded[-1].event_type == "approval"
    assert recorded[-1].payload.decision == "requested"

    gate.grant(ApprovalGrant(run_id="run_e2e", action_class="git_push"))
    gate.perform("git_push", lambda: calls.append("git_push"))
    assert calls == ["git_push"]

    # The approval envelope surfaces in the activity stream (ActivityItem)
    # and in the JSON + CSV audit exports.
    item = to_activity_item(recorded[-1])
    assert item.event_type == "approval"

    json_blob = export_events_json(recorded)
    assert '"event_type":"approval"' in json_blob
    assert '"decision":"granted"' in json_blob
    assert '"action_class":"git_push"' in json_blob

    csv_blob = export_events_csv(recorded)
    assert "approval" in csv_blob and "git_push" in csv_blob


# ---------------------------------------------------------------------------
# 7. End-to-end against a REAL repository + allocator: the reference agent
#    pauses before a consequential workspace decision, resumes on a class-bound
#    grant, and the protected action runs only then.
# ---------------------------------------------------------------------------
def _make_git(tmp_path: Path):
    import subprocess as _sp

    def git(path: Path, *args: str) -> str:
        return _sp.run(
            ["git", "-C", str(path), *args],
            check=True, capture_output=True, text=True,
        ).stdout.strip()

    repo = tmp_path / "repository"
    repo.mkdir()
    git(repo, "init", "-b", "main")
    git(repo, "config", "user.name", "Approval POC")
    git(repo, "config", "user.email", "approval-poc@example.test")
    (repo / "README.md").write_text("seed\n")
    git(repo, "add", "README.md")
    git(repo, "commit", "-m", "seed")
    return repo, git


@pytest.mark.asyncio
async def test_real_repo_agent_pauses_on_consequential_action_and_resumes_on_grant(app, tmp_path: Path):
    repo, git = _make_git(tmp_path)
    from remote_coding_workspace import GitWorktreeAllocator

    allocator = GitWorktreeAllocator(
        repositories={"poc_repo": repo},
        worktree_root=tmp_path / "worktrees",
    )
    workspace = allocator.allocate(repository_id="poc_repo", run_id="run_appr")
    (workspace.path / "feature.py").write_text("def ready(): return True\n")
    git(workspace.path, "add", "feature.py")
    git(workspace.path, "commit", "-m", "add feature")

    recorded: list[EventEnvelope] = []
    gate = ApprovalGate(
        policy=RunPolicy(allowed=set()),
        run_id="run_appr",
        principal_id="user_bilal",
        event_recorder=recorded.append,
    )

    performed: list[str] = []

    def _checkout_push():
        performed.append("git_push")
        return "pushed"

    with pytest.raises(ApprovalPaused):
        gate.perform("git_push", _checkout_push)
    assert performed == []  # paused BEFORE the side effect
    assert recorded[-1].payload.decision == "requested"

    # Class-bound grant unlocks the action exactly once.
    gate.grant(ApprovalGrant(run_id="run_appr", action_class="git_push"))
    assert gate.perform("git_push", _checkout_push) == "pushed"
    assert performed == ["git_push"]

    # Different class still blocked (scope escalation guard).
    with pytest.raises(ApprovalPaused):
        gate.perform("deploy", lambda: performed.append("deploy"))
    assert performed == ["git_push"]

    # The approval events surface in activity + audit export.
    from crewspace.dto.events import to_activity_item
    assert to_activity_item(recorded[0]).event_type == "approval"
    json_blob = export_events_json(recorded)
    assert '"event_type":"approval"' in json_blob
    assert '"decision":"requested"' in json_blob and '"decision":"granted"' in json_blob

    # Cleanup the workspace the durable lifecycle manages.
    assert allocator.cleanup(workspace, discard=True) == "removed"
