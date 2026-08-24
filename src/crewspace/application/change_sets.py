"""Application boundary for team-scoped remote coding change sets."""
from __future__ import annotations

import datetime as dt
import uuid

from .access import can_manage_team
from ..domain.entities import ChangeSetAuditEvent, StoredChangeSet
from ..domain.ports import UnitOfWork
from ..dto.change_sets import ChangeSetDTO


class ChangeSetService:
    GOVERNED_DECISIONS = {
        "request_pr": ("pr_requested", "pr_requested"),
        "retain": ("retain_requested", "retain_requested"),
        "request_discard": ("discard_requested", "discard_requested"),
    }

    async def record_capture(
        self,
        *,
        agent_id: str,
        request_id: str,
        change_set: ChangeSetDTO,
        uow: UnitOfWork,
    ) -> StoredChangeSet:
        run = await uow.coding_runs.get(change_set.run_id)
        if run is None:
            raise KeyError("Coding run not found")
        if run.status != "running":
            raise ValueError("Coding run is not accepting a change set")
        if (
            run.agent_id != agent_id
            or run.request_id != request_id
            or run.repository_id != change_set.repository_id
        ):
            raise PermissionError("Change set does not match the authorized coding run")

        now = dt.datetime.now(dt.timezone.utc)
        change_set_id = f"changeset_{uuid.uuid4().hex[:16]}"
        stored = StoredChangeSet(
            id=change_set_id,
            team_id=run.team_id,
            repository_id=run.repository_id,
            run_id=run.id,
            agent_id=run.agent_id,
            request_id=run.request_id,
            status="captured",
            payload=change_set.model_dump(mode="json"),
            created_at=now,
        )
        event = ChangeSetAuditEvent(
            id=f"csaudit_{uuid.uuid4().hex[:16]}",
            change_set_id=change_set_id,
            action="captured",
            actor_id=agent_id,
            created_at=now,
        )
        await uow.change_sets.create(stored, event)
        await uow.coding_runs.set_status(run.id, "captured")
        return stored

    async def review(
        self, *, change_set_id: str, actor_id: str, uow: UnitOfWork
    ) -> StoredChangeSet:
        stored = await uow.change_sets.get(change_set_id)
        if stored is None:
            raise KeyError("Change set not found")
        now = dt.datetime.now(dt.timezone.utc)
        event = ChangeSetAuditEvent(
            id=f"csaudit_{uuid.uuid4().hex[:16]}",
            change_set_id=stored.id,
            action="reviewed",
            actor_id=actor_id,
            created_at=now,
        )
        transitioned = await uow.change_sets.transition(
            stored.id, expected="captured", status="reviewed", event=event
        )
        if not transitioned:
            raise ValueError("Only a captured change set can be reviewed")
        stored.status = "reviewed"
        return stored

    async def decide(
        self,
        *,
        change_set_id: str,
        decision: str,
        actor_id: str,
        uow: UnitOfWork,
    ) -> StoredChangeSet:
        target = self.GOVERNED_DECISIONS.get(decision)
        if target is None:
            raise ValueError("Unsupported change-set decision")
        stored = await uow.change_sets.get(change_set_id)
        if stored is None:
            raise KeyError("Change set not found")
        status, action = target
        now = dt.datetime.now(dt.timezone.utc)
        event = ChangeSetAuditEvent(
            id=f"csaudit_{uuid.uuid4().hex[:16]}",
            change_set_id=stored.id,
            action=action,
            actor_id=actor_id,
            created_at=now,
        )
        transitioned = await uow.change_sets.transition(
            stored.id, expected="reviewed", status=status, event=event
        )
        if not transitioned:
            raise ValueError("Only a reviewed change set can receive a decision")
        stored.status = status
        return stored

    async def complete_workspace_action(
        self,
        *,
        change_set_id: str,
        expected_status: str,
        status: str,
        action: str,
        agent_id: str,
        uow: UnitOfWork,
    ) -> StoredChangeSet:
        stored = await uow.change_sets.get(change_set_id)
        if stored is None:
            raise KeyError("Change set not found")
        event = ChangeSetAuditEvent(
            id=f"csaudit_{uuid.uuid4().hex[:16]}",
            change_set_id=stored.id,
            action=action,
            actor_id=agent_id,
            created_at=dt.datetime.now(dt.timezone.utc),
        )
        transitioned = await uow.change_sets.transition(
            stored.id,
            expected=expected_status,
            status=status,
            event=event,
        )
        if not transitioned:
            raise ValueError("Change-set workspace action is no longer pending")
        stored.status = status
        return stored


async def execute_workspace_decision(
    *,
    db,
    manager,
    change_set_id: str,
    decision: str,
    current_user: dict,
    timeout: float,
) -> StoredChangeSet:
    action_by_decision = {
        "retain": "retain",
        "request_discard": "discard",
    }
    action = action_by_decision.get(decision)
    if action is None:
        raise ValueError("Unsupported remote workspace decision")

    async with db.uow() as uow:
        candidate = await uow.change_sets.get(change_set_id)
        if candidate is None:
            raise KeyError("Change set not found")
        if not await can_manage_team(current_user, candidate.team_id, uow):
            raise PermissionError("You cannot manage this change set")
        stored = await ChangeSetService().decide(
            change_set_id=change_set_id,
            decision=decision,
            actor_id=current_user["id"],
            uow=uow,
        )
        await uow.commit()

    try:
        change_set = ChangeSetDTO.model_validate(stored.payload)
        result = await manager.send_workspace_action(
            stored.agent_id,
            repository_id=stored.repository_id,
            run_id=stored.run_id,
            branch=change_set.branch,
            action=action,
            timeout=timeout,
        )
        expected_remote_statuses = {
            "retain": {"retained", "already_retained"},
            "discard": {"removed", "already_removed"},
        }
        if result.get("status") not in expected_remote_statuses[action]:
            raise ValueError("Remote workspace action returned an unexpected status")
    except BaseException:
        async with db.uow() as uow:
            await ChangeSetService().complete_workspace_action(
                change_set_id=change_set_id,
                expected_status=f"{action}_requested",
                status="reviewed",
                action="workspace_action_failed",
                agent_id=current_user["id"],
                uow=uow,
            )
            await uow.commit()
        raise
    final_status = "retained" if action == "retain" else "discarded"
    final_audit = "workspace_retained" if action == "retain" else "workspace_discarded"
    expected_status = f"{action}_requested"
    async with db.uow() as uow:
        completed = await ChangeSetService().complete_workspace_action(
            change_set_id=change_set_id,
            expected_status=expected_status,
            status=final_status,
            action=final_audit,
            agent_id=stored.agent_id,
            uow=uow,
        )
        await uow.commit()
    return completed
