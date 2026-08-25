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


# --- Slice 2: wiring into the external MCP execution seam (acceptance item 2) --


async def test_external_mcp_checkpoint_emits_approval_event_before_action(app):
    """A consequential external MCP action emits a canonical `approval`
    (requested) event BEFORE the tool runs, and is blocked fail-closed; an
    allowed run policy lets it proceed and records a `granted` event."""
    from crewspace.application.mcp_tools import build_agent_tool_runtime
    from crewspace.application.tools import ToolPermissionDenied, build_registry
    from crewspace.dto.events import EventEnvelope
    from tests.test_mcp_execution import _seed_approved_tool

    await _seed_approved_tool(app)
    async with app.state.db.uow() as uow:
        await uow.agent_policies.replace_mcp_tools(
            "agent_crewspace", {("mcp_jira", "create_issue")}
        )
        await uow.commit()

    class Executor:
        def __init__(self): self.calls = []
        async def call_tool(self, active_connection, tool_name, arguments):
            self.calls.append((tool_name, arguments))
            return {"issue_key": "ENG-42", "title": arguments["title"]}

    # Blocked path: default-deny policy -> requested event recorded, no execution.
    events_blocked: list[EventEnvelope] = []
    executor = Executor()
    async with app.state.db.uow() as uow:
        runtime = await build_agent_tool_runtime(
            build_registry(), uow,
            principal_id="user_bilal", agent_id="agent_crewspace",
            executor=executor,
            policy=RunPolicy(allowed=set()), run_id="run_x",
            event_recorder=events_blocked.append,
        )
        try:
            await runtime.runner.run("jira.create_issue", title="Denied")
        except ToolPermissionDenied:
            pass
        else:
            raise AssertionError("policy-blocked MCP tool must not execute")
    assert executor.calls == [], "external action must not run when blocked"
    assert len(events_blocked) == 1
    assert events_blocked[0].event_type == "approval"
    assert events_blocked[0].payload.decision == "requested"
    assert events_blocked[0].payload.action_class == "external_mcp"
    assert events_blocked[0].payload.scope == "run_x"
    assert events_blocked[0].payload.principal_id == "user_bilal"

    # Allowed path: policy allows external_mcp -> granted event, tool runs.
    events_allowed: list[EventEnvelope] = []
    executor2 = Executor()
    async with app.state.db.uow() as uow:
        runtime = await build_agent_tool_runtime(
            build_registry(), uow,
            principal_id="user_bilal", agent_id="agent_crewspace",
            executor=executor2,
            policy=RunPolicy(allowed={"external_mcp"}), run_id="run_x",
            event_recorder=events_allowed.append,
        )
        result = await runtime.runner.run("jira.create_issue", title="Ship")
    assert executor2.calls == [("create_issue", {"title": "Ship"})]
    assert result == {"issue_key": "ENG-42", "title": "Ship"}
    assert len(events_allowed) == 1
    assert events_allowed[0].payload.decision == "granted"
    assert events_allowed[0].payload.action_class == "external_mcp"


# --- Slice 3: prior approval decision honored at the seam (acceptance items 3-4) --


async def test_prior_approval_decision_is_honored_fail_closed(app):
    """With a policy that would allow the action, a prior `granted` decision
    drives execution; a prior `denied`/`expired`/`requested` decision blocks the
    action fail-closed (no execution) and the recorded event reflects the
    fail-closed decision. The decision is tied to the run + action class."""
    from crewspace.application.mcp_tools import build_agent_tool_runtime
    from crewspace.application.tools import ToolPermissionDenied, build_registry
    from crewspace.dto.events import EventEnvelope
    from tests.test_mcp_execution import _seed_approved_tool

    await _seed_approved_tool(app)
    async with app.state.db.uow() as uow:
        await uow.agent_policies.replace_mcp_tools(
            "agent_crewspace", {("mcp_jira", "create_issue")}
        )
        await uow.commit()

    class Executor:
        def __init__(self): self.calls = []
        async def call_tool(self, active_connection, tool_name, arguments):
            self.calls.append((tool_name, arguments))
            return {"issue_key": "ENG-42", "title": arguments["title"]}

    async def run_with(decision):
        events: list[EventEnvelope] = []
        executor = Executor()
        async with app.state.db.uow() as uow:
            runtime = await build_agent_tool_runtime(
                build_registry(), uow,
                principal_id="user_bilal", agent_id="agent_crewspace",
                executor=executor,
                policy=RunPolicy(allowed={"external_mcp"}), run_id="run_x",
                event_recorder=events.append,
                approval_decision=decision,
            )
            ran = False
            try:
                await runtime.runner.run("jira.create_issue", title="X")
                ran = True
            except ToolPermissionDenied:
                pass
        return ran, executor.calls, events

    # prior granted -> executes (the granted decision drives the action)
    ran, calls, events = await run_with("granted")
    assert ran is True
    assert calls == [("create_issue", {"title": "X"})]
    assert events and events[0].payload.decision == "granted"
    assert events[0].payload.action_class == "external_mcp"
    assert events[0].payload.scope == "run_x"

    # prior denied -> blocked fail-closed, no execution
    ran, calls, events = await run_with("denied")
    assert ran is False
    assert calls == []
    assert events and events[0].payload.decision == "requested"
    assert events[0].payload.action_class == "external_mcp"

    # prior expired -> blocked fail-closed, no execution
    ran, calls, events = await run_with("expired")
    assert ran is False
    assert calls == []
    assert events and events[0].payload.decision == "requested"
    assert events[0].payload.action_class == "external_mcp"

    # prior requested (unresolved) -> blocked fail-closed, no execution
    ran, calls, events = await run_with("requested")
    assert ran is False
    assert calls == []
    assert events and events[0].payload.decision == "requested"
    assert events[0].payload.action_class == "external_mcp"


# --- Slice 4: decision bound to (run, principal, action class) (acceptance item 4) --


def test_approval_decision_is_bound_to_run_principal_and_action_class():
    """A granted approval is scoped to the exact (run, principal, action class):
    it unlocks only that action class for that run/principal and cannot be used
    to escalate to a different class. The recorded event binds all three."""
    from crewspace.dto.events import EventEnvelope

    policy = RunPolicy(allowed={"external_mcp"})

    # granted for external_mcp (class present in approved_for) -> unlocks it
    granted = evaluate_action(
        policy, "external_mcp", "run_x", "user_bilal",
        approved_for={"external_mcp"}, prior_decision="granted",
    )
    assert granted.allowed is True
    assert granted.event.payload.decision == "granted"
    assert granted.event.payload.action_class == "external_mcp"
    assert granted.event.payload.scope == "run_x"
    assert granted.event.payload.principal_id == "user_bilal"

    # SCOPE ESCALATION: same granted prior, different class -> blocked.
    escalated = evaluate_action(
        policy, "shell_command", "run_x", "user_bilal",
        approved_for={"external_mcp"}, prior_decision="granted",
    )
    assert escalated.allowed is False
    assert escalated.event.payload.decision == "requested"
    assert escalated.event.payload.action_class == "shell_command"
    assert escalated.event.payload.scope == "run_x"
    assert escalated.event.payload.principal_id == "user_bilal"

    # Different principal on the SAME run/class -> independent decision.
    other_principal = evaluate_action(
        policy, "external_mcp", "run_x", "user_other",
        approved_for={"external_mcp"}, prior_decision=None,
    )
    assert other_principal.allowed is True  # policy allows it; principal differs
    assert other_principal.event.payload.principal_id == "user_other"
    assert other_principal.event.payload.scope == "run_x"
    assert other_principal.event.payload.action_class == "external_mcp"

    # Different run on the SAME class/principal -> independent event binding.
    other_run = evaluate_action(
        policy, "external_mcp", "run_y", "user_bilal",
        approved_for={"external_mcp"}, prior_decision="granted",
    )
    assert other_run.allowed is True
    assert other_run.event.payload.scope == "run_y"

    # Each outcome is an independently-bound canonical event.
    assert isinstance(granted.event, EventEnvelope)
    assert isinstance(escalated.event, EventEnvelope)


# --- Slice 5: denied/expired/replayed approvals cannot execute (acceptance item 5) --


async def test_denied_or_expired_approval_cannot_be_replayed_to_execute(app):
    """A denied/expired approval decision cannot be replayed to unlock the
    protected action: re-submitting the same (replayed) prior decision blocks
    the action again, and the external tool never executes. A granted decision
    cannot be replayed across a different run/principal/action binding."""
    from crewspace.application.mcp_tools import build_agent_tool_runtime
    from crewspace.application.tools import ToolPermissionDenied, build_registry
    from crewspace.dto.events import EventEnvelope
    from tests.test_mcp_execution import _seed_approved_tool

    await _seed_approved_tool(app)
    async with app.state.db.uow() as uow:
        await uow.agent_policies.replace_mcp_tools(
            "agent_crewspace", {("mcp_jira", "create_issue")}
        )
        await uow.commit()

    class Executor:
        def __init__(self): self.calls = []
        async def call_tool(self, active_connection, tool_name, arguments):
            self.calls.append((tool_name, arguments))
            return {"issue_key": "ENG-42", "title": arguments["title"]}

    async def run_with(decision):
        events: list[EventEnvelope] = []
        executor = Executor()
        async with app.state.db.uow() as uow:
            runtime = await build_agent_tool_runtime(
                build_registry(), uow,
                principal_id="user_bilal", agent_id="agent_crewspace",
                executor=executor,
                policy=RunPolicy(allowed={"external_mcp"}), run_id="run_x",
                event_recorder=events.append,
                approval_decision=decision,
            )
            ran = False
            try:
                await runtime.runner.run("jira.create_issue", title="X")
                ran = True
            except ToolPermissionDenied:
                pass
        return ran, executor.calls, events

    # Replay a denied decision twice: both attempts blocked, no execution.
    for attempt in range(2):
        ran, calls, events = await run_with("denied")
        assert ran is False, f"denied replay attempt {attempt} must block"
        assert calls == [], "executor must never run on a denied decision"
        assert events and events[0].payload.decision == "requested"

    # Replay an expired decision twice: both blocked.
    for attempt in range(2):
        ran, calls, events = await run_with("expired")
        assert ran is False, f"expired replay attempt {attempt} must block"
        assert calls == []
        assert events and events[0].payload.decision == "requested"

    # A granted decision for run_x/external_mcp MUST NOT be replayable to unlock
    # a different class (shell_command) even if the policy allows it.
    from crewspace.application.run_policy import evaluate_action

    granted_ext = evaluate_action(
        RunPolicy(allowed={"external_mcp", "shell_command"}),
        "shell_command", "run_x", "user_bilal",
        approved_for={"external_mcp"}, prior_decision="granted",
    )
    assert granted_ext.allowed is False, "granted-for-external_mcp cannot replay to shell_command"

    # And a granted decision for run_x MUST NOT unlock a different run unless
    # that run's approved_for also carries the class (caller-scoped, not auto).
    granted_run_y = evaluate_action(
        RunPolicy(allowed={"external_mcp"}),
        "external_mcp", "run_y", "user_bilal",
        approved_for=set(), prior_decision="granted",
    )
    assert granted_run_y.allowed is False, "granted-for-run_x cannot replay to run_y without its own approval"


# --- Slice 6: every approval is an auditable canonical event (acceptance item 6) --


async def test_approval_event_surfaces_in_activity_and_audit_export(app):
    """Each checkpoint outcome is emitted as a canonical `approval` EventEnvelope
    that surfaces in the M6.4 activity stream + audit export end-to-end: the
    recorded envelopes serialize through export_events_json/csv, and merge with
    a run's run_to_events output in the unified canonical export."""
    from crewspace.application.mcp_tools import build_agent_tool_runtime
    from crewspace.application.tools import ToolPermissionDenied, build_registry
    from crewspace.dto.events import (
        EventEnvelope, export_events_csv, export_events_json, run_to_events,
    )
    from tests.test_mcp_execution import _seed_approved_tool

    await _seed_approved_tool(app)
    async with app.state.db.uow() as uow:
        await uow.agent_policies.replace_mcp_tools(
            "agent_crewspace", {("mcp_jira", "create_issue")}
        )
        await uow.commit()

    recorded: list[EventEnvelope] = []

    class Executor:
        async def call_tool(self, active_connection, tool_name, arguments):
            return {"issue_key": "ENG-42", "title": arguments["title"]}

    # Run an external MCP action under a policy that blocks it (default-deny):
    # the checkpoint emits a `requested` approval event (recorded) and blocks.
    async with app.state.db.uow() as uow:
        runtime = await build_agent_tool_runtime(
            build_registry(), uow,
            principal_id="user_bilal", agent_id="agent_crewspace",
            executor=Executor(),
            policy=RunPolicy(allowed=set()), run_id="run_x",
            event_recorder=recorded.append,
        )
        try:
            await runtime.runner.run("jira.create_issue", title="X")
        except ToolPermissionDenied:
            pass
    assert len(recorded) == 1
    approval = recorded[0]
    assert isinstance(approval, EventEnvelope)
    assert approval.event_type == "approval"
    assert approval.payload.decision == "requested"
    assert approval.payload.action_class == "external_mcp"
    assert approval.payload.scope == "run_x"

    # Audit export: the approval canonical event serializes through JSON + CSV.
    json_blob = export_events_json(recorded)
    assert '"event_type":"approval"' in json_blob
    assert '"decision":"requested"' in json_blob
    assert '"action_class":"external_mcp"' in json_blob
    csv_blob = export_events_csv(recorded)
    assert "approval" in csv_blob and "requested" in csv_blob

    # Unified export: the approval merges with the run's own lifecycle events.
    # Build a minimal run-like object exposing the fields run_to_events reads.
    class _Run:
        id = "run_x"
        team_id = "team_1"
        repository_id = "repo_1"
        agent_id = "agent_crewspace"
        request_id = "req_1"
        instruction = "ship it"
        status = "queued"
        created_at = None
        started_at = None
        finished_at = None
        failure_reason = None
        recent_output = None
    run_events = run_to_events(_Run())
    unified = [*run_events, *recorded]
    assert any(
        e.event_type == "approval" and e.payload.action_class == "external_mcp"
        for e in unified
    )
    unified_json = export_events_json(unified)
    assert unified_json.count('"event_type":"approval"') == 1
