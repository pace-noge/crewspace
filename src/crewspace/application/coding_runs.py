"""Application boundary for durable, authorization-scoped coding-run dispatch.

A coding run is created on the control plane only from identity derived from the
authenticated session (team_id, requested_by). The remote agent never supplies
those fields; it only receives opaque repository id, run id, instruction, and a
correlation request_id that the control plane generated and persisted on the run.
"""
from __future__ import annotations

import datetime as dt
import uuid

from ..domain.entities import CodingRun
from ..domain.ports import UnitOfWork


async def dispatch_coding_run(
    uow: UnitOfWork,
    *,
    agent_id: str,
    team_id: str,
    repository_id: str,
    run_id: str,
    instruction: str,
    requested_by: str,
    agent_manager=None,
    timeout: float = 300.0,
) -> CodingRun:
    """Persist an authorization-scoped run and dispatch it to the agent.

    Identity (team_id, requested_by) must come from authenticated state at the
    call site. The team<->repository grant is re-checked here via the contracted
    CodingRepositoryRepository.is_team_granted (not merely delegated to the run
    repository's create), so an unauthorized dispatch fails closed before any
    frame leaves the control plane regardless of the repository implementation.
    A distinct correlation request_id is generated, persisted on the run, and
    sent to the agent so the returned change set satisfies the ownership check.
    The run moves queued -> running within this unit of work so the agent's
    returned change set (which the capture path only accepts for a running run)
    can complete the lifecycle.
    """
    from ..api.connection import AgentConnectionManager
    from ..api.connection import agent_manager as default_manager

    if not await uow.coding_repositories.is_team_granted(team_id, repository_id):
        raise PermissionError("Team is not authorized for this repository")

    now = dt.datetime.now(dt.timezone.utc)
    request_id = uuid.uuid4().hex
    run = CodingRun(
        id=run_id,
        team_id=team_id,
        repository_id=repository_id,
        requested_by=requested_by,
        agent_id=agent_id,
        request_id=request_id,
        instruction=instruction,
        status="queued",
        created_at=now,
        updated_at=now,
    )
    created = await uow.coding_runs.create(run)
    started = await uow.coding_runs.transition(
        run_id,
        expected="queued",
        status="running",
        updated_at=now,
        started_at=now,
        finished_at=None,
    )
    if not started:
        raise RuntimeError("Coding run did not enter the running state")
    await uow.commit()
    dispatched = await uow.coding_runs.get(run_id) or created

    manager: AgentConnectionManager = agent_manager or default_manager
    await manager.send_coding_run(
        agent_id,
        repository_id=repository_id,
        run_id=run_id,
        instruction=instruction,
        timeout=timeout,
        request_id=request_id,
    )
    return dispatched


async def cancel_coding_run(
    uow: UnitOfWork,
    *,
    run_id: str,
    requested_by: str,
    agent_manager=None,
) -> bool:
    """Fail-closed cancel of a durable coding run.

    Re-checks the run exists, transitions it to ``cancelled`` only from a
    cancellable state (queued/running) via the contracted CAS transition, records
    finished_at, and then dispatches a cancellation command to the agent over the
    authenticated socket. Idempotent: an already-terminal run returns False and no
    frame is dispatched. Unknown runs raise KeyError (the endpoint maps this to
    404). Authorization is enforced by the caller (team membership) before this
    boundary is reached.
    """
    from ..api.connection import AgentConnectionManager
    from ..api.connection import agent_manager as default_manager

    run = await uow.coding_runs.get(run_id)
    if run is None:
        raise KeyError(run_id)

    # Fail-closed: only a live run (queued/running) can be cancelled. An already
    # terminal run is not re-transitioned and no frame is dispatched.
    if run.status not in ("queued", "running"):
        return False

    # Fail-closed ordering: dispatch the cancellation command to the agent BEFORE
    # we persist the terminal state. If the agent cannot receive it (disconnected
    # or lacks the capability), send raises and we do NOT mark the run cancelled
    # for a subprocess we never asked to stop.
    manager: AgentConnectionManager = agent_manager or default_manager
    await manager.send_coding_cancel(
        run.agent_id,
        run_id=run.id,
        request_id=run.request_id,
    )

    now = dt.datetime.now(dt.timezone.utc)
    cancelled = await uow.coding_runs.transition(
        run_id,
        expected=run.status,
        status="cancelled",
        updated_at=now,
        started_at=run.started_at,
        finished_at=now,
    )
    if not cancelled:
        return False
    await uow.commit()
    return True
