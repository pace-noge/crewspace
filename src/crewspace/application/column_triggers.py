"""Application orchestration for board column→workflow triggers."""
from __future__ import annotations

import uuid

from ..domain.ports import UnitOfWork
from .workflows import WorkflowService


async def trigger_column_workflow(
    *, card, target_column_id: str, uow: UnitOfWork, actor_id: str | None,
    event_key: str,
):
    """Dispatch one configured move-in workflow, fail-closed and idempotently.

    The caller must already have authorized the card move. This seam independently
    verifies rule/card board equality, rule/workflow availability, and atomically
    claims (card, column, workflow) before dispatch so HTTP and tool paths cannot
    double-enqueue the same move.
    """
    rule = await uow.boards.get_column_workflow(target_column_id)
    if rule is None or not rule.enabled:
        return None
    card_board_id = await uow.boards.get_board_id_for_card(card.id)
    if card_board_id is None or rule.board_id != card_board_id:
        return None
    workflow = await uow.workflows.get(rule.workflow_id)
    if workflow is None or not workflow.enabled:
        return None
    board = await uow.boards.get_board(rule.board_id)
    workflow_channel = await uow.channels.get_channel(workflow.channel_id)
    if (
        board is None
        or workflow_channel is None
        or workflow_channel.workspace_id != board.workspace_id
    ):
        return None
    trigger_id = f"cmt_{uuid.uuid4().hex[:16]}"
    claimed = await uow.boards.claim_column_move_trigger(
        trigger_id=trigger_id,
        card_id=card.id,
        column_id=target_column_id,
        workflow_id=workflow.id,
        board_id=rule.board_id,
        event_key=event_key,
    )
    if not claimed:
        return None
    event = {
        "card_id": card.id,
        "card_title": card.title,
        "column_id": target_column_id,
        "board_id": rule.board_id,
        "actor_id": actor_id,
    }
    run = await WorkflowService().run(
        workflow, uow, event, trigger_type="column_move"
    )
    await uow.boards.bind_column_move_trigger(trigger_id, run.id)
    return run


async def board_workflow_badges(
    uow: UnitOfWork, board_id: str
) -> dict[str, list[dict[str, str]]]:
    """Canonical render-ready workflow badges keyed by card id."""
    badges: dict[str, list[dict[str, str]]] = {}
    for status in await uow.boards.list_board_column_move_statuses(board_id):
        badges.setdefault(status.card_id, []).append(
            {
                "label": f"Workflow: {status.run_status}",
                "href": f"/workflows/{status.workflow_id}",
                "title": status.workflow_name,
            }
        )
    return badges
