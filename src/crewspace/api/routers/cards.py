"""API: card router — move + comment (HTMX fragments)."""
from __future__ import annotations

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse

from ..deps import BoardServiceDep, ChatServiceDep, CurrentUserDep, UowDep
from ..connection import manager
from ...domain.identifiers import DEFAULT_CHANNEL_ID
from ..rendering import templates
from ...application.access import require_board_access

router = APIRouter(prefix="/cards", tags=["card"])


@router.post("/{card_id}/move", response_class=HTMLResponse)
async def move_card(
    request: Request,
    card_id: str,
    svc: BoardServiceDep,
    chat_svc: ChatServiceDep,
    uow: UowDep,
    current_user: CurrentUserDep,
    column_id: str = Form(...),
) -> HTMLResponse:
    board_id = await uow.boards.get_board_id_for_card(card_id)
    target_board_id = await uow.boards.get_board_id_for_column(column_id)
    if board_id is None or target_board_id != board_id:
        raise HTTPException(status_code=404, detail="card not found")
    await require_board_access(current_user, board_id, uow)
    if await uow.boards.is_column_archived(column_id):
        raise HTTPException(status_code=404, detail="column not found")
    old_column_id, card = await svc.move_card(card_id, column_id, uow, actor_id=current_user["id"])
    if card is None:
        raise HTTPException(status_code=404, detail="card not found")
    # Re-render the WHOLE board from the source of truth and swap #board-wrap.
    # This is bulletproof: both the source and destination columns update, so a
    # moved card can never be left behind in its old column (no OOB ambiguity).
    board = await svc.get_board(board_id, uow)
    if board is None:
        raise HTTPException(status_code=404, detail="board not found")
    # Announce the move in chat (planner), crediting the actor.
    col_name = {c.id: c.name for c in board.columns}
    old_name = col_name.get(old_column_id, old_column_id or "?")
    new_name = col_name.get(card.column_id, card.column_id)
    ann = await chat_svc.announce(
        DEFAULT_CHANNEL_ID,
        f"🔄 {current_user['name']} moved \"{card.title}\" from {old_name} → {new_name}",
        uow,
    )
    await manager.broadcast(DEFAULT_CHANNEL_ID, ann.model_dump(mode="json"))
    return templates.TemplateResponse(request=request, name="board_fragment.html", context={"board": board})


@router.post("/{card_id}/comments", response_class=HTMLResponse)
async def add_comment(
    request: Request,
    card_id: str,
    svc: BoardServiceDep,
    uow: UowDep,
    current_user: CurrentUserDep,
    body: str = Form(...),
) -> HTMLResponse:
    board_id = await uow.boards.get_board_id_for_card(card_id)
    if board_id is None:
        raise HTTPException(status_code=404, detail="card not found")
    await require_board_access(current_user, board_id, uow)
    comment = await svc.comment_card(card_id, current_user["id"], body, uow)
    return templates.TemplateResponse(request=request, name="comment.html", context={"comment": comment})
