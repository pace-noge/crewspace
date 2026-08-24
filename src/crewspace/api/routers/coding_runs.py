"""Authenticated control-plane endpoint that starts a durable coding run.

The caller's identity (requested_by) and authorization scope (team_id) are
derived from the authenticated session, never trusted from the request body.
The team must be granted the target repository, and the principal must be able
to manage that team, before any run is persisted or dispatched.
"""
from __future__ import annotations

import uuid

from fastapi import APIRouter, HTTPException

from ...application.access import can_manage_team
from ...application.coding_runs import dispatch_coding_run
from ..deps import CurrentUserDep, UowDep

router = APIRouter(prefix="/api/coding/runs", tags=["coding-runs"])


@router.post("")
async def start_coding_run(
    payload: dict,
    user: CurrentUserDep,
    uow: UowDep,
) -> dict:
    repository_id = payload.get("repository_id")
    agent_id = payload.get("agent_id")
    instruction = payload.get("instruction")
    team_id = payload.get("team_id")

    if not isinstance(repository_id, str) or not repository_id.strip():
        raise HTTPException(status_code=422, detail="repository_id is required")
    if not isinstance(agent_id, str) or not agent_id.strip():
        raise HTTPException(status_code=422, detail="agent_id is required")
    if not isinstance(instruction, str) or not instruction.strip() or len(instruction) > 65_536:
        raise HTTPException(status_code=422, detail="instruction is required")
    if not isinstance(team_id, str) or not team_id.strip():
        raise HTTPException(status_code=422, detail="team_id is required")

    if not await can_manage_team(user, team_id, uow):
        raise HTTPException(status_code=403, detail="Not authorized for this team")

    if not await uow.coding_repositories.is_team_granted(team_id, repository_id):
        raise HTTPException(status_code=403, detail="Team is not granted this repository")

    run_id = uuid.uuid4().hex
    await dispatch_coding_run(
        uow,
        agent_id=agent_id,
        team_id=team_id,
        repository_id=repository_id,
        run_id=run_id,
        instruction=instruction,
        requested_by=user["id"],
    )
    run = await uow.coding_runs.get(run_id)
    return {"run_id": run_id, "status": run.status if run else "running"}
