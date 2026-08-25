"""Authenticated control-plane endpoint that starts a durable coding run.

The caller's identity (requested_by) and authorization scope (team_id) are
derived from the authenticated session, never trusted from the request body.
The team must be granted the target repository, and the principal must be able
to manage that team, before any run is persisted or dispatched.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException

from ...application.access import can_manage_team, is_team_member
from ...application.coding_runs import cancel_coding_run, dispatch_coding_run
from ...dto.events import run_to_activity
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


@router.get("/{run_id}")
async def get_coding_run(run_id: str, user: CurrentUserDep, uow: UowDep) -> dict:
    run = await uow.coding_runs.get(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="Coding run not found")
    if not await is_team_member(user, run.team_id, uow):
        raise HTTPException(status_code=403, detail="Not authorized for this team")

    timeline = [{"event": "created", "at": run.created_at.isoformat()}]
    if run.started_at:
        timeline.append({"event": "started", "at": run.started_at.isoformat()})
    if run.finished_at:
        timeline.append({"event": "finished", "at": run.finished_at.isoformat()})

    duration_ms = None
    if run.started_at:
        end = run.finished_at or datetime.now(timezone.utc)
        duration_ms = int((end - run.started_at).total_seconds() * 1000)

    result: dict = {"status": run.status}
    if run.status == "failed":
        result["failure_reason"] = run.failure_reason
    elif run.status == "succeeded":
        change_set = await uow.change_sets.get_by_run_id(run.id)
        if change_set is not None:
            result["change_set_id"] = change_set.id

    return {
        "run_id": run.id,
        "team_id": run.team_id,
        "repository_id": run.repository_id,
        "agent_id": run.agent_id,
        "request_id": run.request_id,
        "instruction": run.instruction,
        "status": run.status,
        "created_at": run.created_at.isoformat(),
        "started_at": run.started_at.isoformat() if run.started_at else None,
        "finished_at": run.finished_at.isoformat() if run.finished_at else None,
        "recent_output": run.recent_output,
        "failure_reason": run.failure_reason,
        "timeline": timeline,
        "duration_ms": duration_ms,
        "result": result,
        # Compact typed activity (M6.4 item 4): derived from the run's real
        # fields; raw logs remain available on demand via recent_output.
        "activity": [item.model_dump(mode="json") for item in run_to_activity(run)],
        "has_raw_logs": bool(run.recent_output),
    }


@router.post("/{run_id}/cancel")
async def cancel_coding_run_endpoint(run_id: str, user: CurrentUserDep, uow: UowDep) -> dict:
    run = await uow.coding_runs.get(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="Coding run not found")
    if not await is_team_member(user, run.team_id, uow):
        raise HTTPException(status_code=403, detail="Not authorized for this team")
    try:
        cancelled = await cancel_coding_run(
            uow, run_id=run_id, requested_by=user["id"]
        )
    except KeyError:
        raise HTTPException(status_code=404, detail="Coding run not found")
    except RuntimeError as exc:
        # The agent could not be asked to stop (disconnected or lacks the
        # cancellation capability). The run is NOT marked cancelled, since we
        # never delivered the stop command to a live subprocess.
        raise HTTPException(status_code=409, detail=f"cancel failed: {exc}")
    if not cancelled:
        # Already terminal; report current status without re-dispatching a frame.
        current = await uow.coding_runs.get(run_id)
        return {"run_id": run_id, "status": current.status if current else "unknown"}
    return {"run_id": run_id, "status": "cancelled"}
