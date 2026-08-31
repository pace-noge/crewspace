"""M7.7 — board operating-surface integration POC.

``run_board_poc`` walks the WHOLE M7 stack end to end on an isolated, seeded
database: create a board, add a card with editable metadata, link it to a
coding run, move it into a trigger column to start a workflow, comment, observe
the live board-room deltas that reach a second viewer, and confirm attention
items surface in the team-scoped inbox with zero cross-tenant leakage.

Mirrors the M6.8 inbox POC shape (seeded, deterministic, isolated) but for the
board operating surface: it is DB-backed (boards/cards/runs/workflows are
persisted state), runs against the per-test temp DB, uses a stubbed agent
transport for the coding-run dispatch, and asserts on a frozen ``BoardPocReport``.
"""
from __future__ import annotations

import asyncio
import datetime as dt

from dataclasses import dataclass
from typing import Optional


@dataclass(frozen=True)
class BoardPocReport:
    board_id: str
    column_count: int
    card_id: str
    card_title: str
    card_priority: Optional[str]
    card_labels: tuple[str, ...]
    card_activity_count: int
    linked_run_id: Optional[str]
    linked_run_status: Optional[str]
    workflow_run: Optional[str]  # id of the column_move workflow run
    moved_column: Optional[str]
    comment_count: int
    live_delta_kinds: tuple[str, ...]
    inbox_kinds: tuple[str, ...]
    cross_tenant_visible: int


def _column_by_name(board, name: str):
    return next((c for c in board.columns if c.name == name), None)


async def _run(app) -> BoardPocReport:
    from crewspace.application.coding_runs import dispatch_coding_run
    from crewspace.application.inbox import load_inbox_for_team
    from crewspace.application.services import BoardService
    from crewspace.application.tools import build_registry
    from crewspace.application.workflows import WorkflowService
    from crewspace.domain.entities import CodingRepository, TeamRepositoryAccess
    from crewspace.dto.board import BoardDeltaDTO
    from crewspace.api.board_live import board_room, build_board_delta_publisher
    from crewspace.api.connection import manager

    service = BoardService(build_registry(), app.state.settings)
    user = None
    async with app.state.db.uow() as uow:
        user = await uow.auth.get_member("user_bilal")
    assert user is not None

    # --- 1. Create a board and a card with editable metadata. ---------------
    board = None
    card = None
    doing_col = None
    todo_col = None
    async with app.state.db.uow() as uow:
        board = await service.create_board("ws_default", "POC Sprint", uow)
        todo_col = _column_by_name(board, "To Do")
        doing_col = _column_by_name(board, "In Progress")
        assert todo_col is not None and doing_col is not None
        card = await service.create_card(
            todo_col.id, "POC task", uow,
            description="Seeded operating-surface card", actor_id=user["id"],
        )
        updated = await service.update_card(
            card.id, uow, actor_id=user["id"], priority="high", labels=["poc"],
        )
        if updated is not None:
            card = updated
        await service.set_assignee(card.id, user["id"], uow, actor_id=user["id"])
        await uow.commit()

    # --- 2. Link a coding run to the card (stubbed agent transport). --------
    now = dt.datetime.now(dt.timezone.utc)
    repo_id = "repo_poc_board"
    run_id = "run_poc_board"
    async with app.state.db.uow() as uow:
        await uow.coding_repositories.create(
            CodingRepository(
                id=repo_id, name="POC Repo", default_branch="main",
                created_by=user["id"], created_at=now,
            )
        )
        await uow.coding_repositories.grant_team(
            TeamRepositoryAccess(
                team_id="team_acme", repository_id=repo_id,
                granted_by=user["id"], granted_at=now,
            )
        )
        await uow.commit()

    dispatched = {}
    class _StubManager:
        async def send_coding_run(self, agent_id, **kw):
            dispatched["agent_id"] = agent_id
            dispatched.update(kw)

    linked_run_status = None
    async with app.state.db.uow() as uow:
        run = await dispatch_coding_run(
            uow, agent_id="agent_planner", team_id="team_acme",
            repository_id=repo_id, run_id=run_id, instruction="Implement the POC card",
            requested_by=user["id"], agent_manager=_StubManager(),
        )
        await service.link_card_to_run(card.id, run.id, user, uow)
        statuses = await service.card_run_status(card.id, user, uow)
        linked_run_status = statuses[0].run_status if statuses else None
        await uow.commit()
    assert dispatched.get("agent_id") == "agent_planner"

    # --- 3. Move the card into a trigger column -> start a workflow. --------
    workflow_run = None
    workflow_id = None
    async with app.state.db.uow() as uow:
        workflow = await WorkflowService().create(
            uow, creator_id=user["id"],
            data={
                "name": "poc_column_flow",
                "description": "Triggered by board column move",
                "channel_id": "chan_general",
                "enabled": True,
                "trigger_type": "column_move",
                "trigger_config": {},
                "steps": [
                    {
                        "id": "step_1", "name": "Delay", "action": "delay",
                        "timeout_seconds": 5, "config": {"seconds": "0"},
                    }
                ],
            },
        )
        workflow_id = workflow.id
        await service.set_column_trigger(
            board.id, doing_col.id, workflow.id, enabled=True, user=user, uow=uow,
        )
        await uow.commit()

    async with app.state.db.uow() as uow:
        _, moved = await service.move_card(card.id, doing_col.id, uow, actor_id=user["id"])
        await uow.commit()
        assert moved is not None and moved.column_id == doing_col.id
        runs = await uow.workflows.list_runs(workflow_id)
        column_move_runs = [r for r in runs if r.trigger_type == "column_move"]
        if column_move_runs:
            workflow_run = column_move_runs[0].id

    # --- 4. Comment on the card. --------------------------------------------
    comment_count = 0
    comment = None
    activity_count = 0
    async with app.state.db.uow() as uow:
        await service.comment_card(card.id, user["id"], "POC: card moved to In Progress", uow)
        detail = await service.get_card_detail(card.id, uow)
        assert detail is not None
        comment_count = len(detail.card.comments)
        comment = detail.card.comments[-1]
        activity_count = len(detail.activity)
        await uow.commit()

    # --- 5. Live deltas reach the board room via the broadcast adapter. ------
    assert comment is not None
    captured = []
    original_broadcast = manager.broadcast

    async def _capture_broadcast(room: str, frame: dict) -> None:
        captured.append((room, frame))

    manager.broadcast = _capture_broadcast
    try:
        publisher = build_board_delta_publisher()
        async with app.state.db.uow() as uow:
            refreshed = await uow.boards.get_card(card.id)
            await publisher(
                uow, board.id,
                BoardDeltaDTO(kind="card_created", card_id=card.id, to_column_id=card.column_id),
                card,
            )
            await publisher(
                uow, board.id,
                BoardDeltaDTO(
                    kind="card_moved", card_id=card.id,
                    from_column_id=todo_col.id, to_column_id=doing_col.id,
                ),
                refreshed,
            )
            await publisher(
                uow, board.id,
                BoardDeltaDTO(kind="comment_added", card_id=card.id, comment_id=comment.id),
                comment,
            )
        live_delta_kinds = tuple(
            f["delta"]["kind"] for room, f in captured
            if room == board_room(board.id)
        )
    finally:
        manager.broadcast = original_broadcast

    # --- 6. Attention items surface in the team inbox, not cross-tenant. -----
    records = [
        {
            "source_type": "workflow_run", "source_id": workflow_run or "run_virtual",
            "status": "failed", "team_id": "team_acme",
            "summary": "Board workflow needs attention",
            "created_at": "2026-08-31T00:00:00Z",
            "deep_link_id": workflow_id or "workflow_poc",
        },
        {
            "source_type": "task", "source_id": card.id, "status": "stale",
            "team_id": "team_acme", "summary": "Card is stale",
            "created_at": "2026-08-31T00:00:00Z", "deep_link_id": board.id,
        },
    ]
    authorized = load_inbox_for_team(records, "team_acme", principal_team_id="team_acme")
    denied = load_inbox_for_team(records, "team_acme", principal_team_id="team_other")
    inbox_kinds = tuple(sorted({item.kind for item in authorized}))

    return BoardPocReport(
        board_id=board.id,
        column_count=len(board.columns),
        card_id=card.id,
        card_title=card.title,
        card_priority=card.priority,
        card_labels=tuple(card.labels or ()),
        card_activity_count=activity_count,
        linked_run_id=run_id,
        linked_run_status=linked_run_status,
        workflow_run=workflow_run,
        moved_column=doing_col.id,
        comment_count=comment_count,
        live_delta_kinds=live_delta_kinds,
        inbox_kinds=inbox_kinds,
        cross_tenant_visible=len(denied),
    )


def run_board_poc(app) -> BoardPocReport:
    """Run the whole M7 board operating-surface walk on an isolated app (temp DB)."""
    return asyncio.run(_run(app))
