"""API: board router — board page + card creation (HTMX fragments)."""
from __future__ import annotations

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse

from ...domain.identifiers import DEFAULT_CHANNEL_ID, DEFAULT_BOARD_ID
from ...domain.ports import UnitOfWork
from ..connection import manager
from ..deps import BoardServiceDep, ChatServiceDep, CurrentUserDep, UowDep
from ..rendering import templates
from ...application.services import BoardService, ChatService
from ...application.access import require_board_access

router = APIRouter(prefix="/boards", tags=["board"])


@router.get("/{board_id}", response_class=HTMLResponse)
async def board_page(
    request: Request, board_id: str, svc: BoardServiceDep, uow: UowDep, current_user: CurrentUserDep
) -> HTMLResponse:
    await require_board_access(current_user, board_id, uow)
    board = await svc.get_board(board_id, uow)
    if board is None:
        raise HTTPException(status_code=404, detail="board not found")
    agents = await uow.auth.list_members(kind="agent")
    return templates.TemplateResponse(
        request=request, name="board.html", context={"board": board, "current_user": current_user, "agents": agents}
    )


@router.get("/{board_id}/columns/{column_id}", response_class=HTMLResponse)
async def column_fragment(
    request: Request, board_id: str, column_id: str, svc: BoardServiceDep,
    uow: UowDep, current_user: CurrentUserDep,
) -> HTMLResponse:
    """Standalone single-column fragment (debug / inspection)."""
    await require_board_access(current_user, board_id, uow)
    column = await svc.get_column(board_id, column_id, uow)
    if column is None:
        raise HTTPException(status_code=404, detail="column not found")
    return templates.TemplateResponse(
        request=request, name="column.html", context={"board_id": board_id, "board": column, "column": column}
    )


@router.post("/{board_id}/cards", response_class=HTMLResponse)
async def create_card(
    request: Request,
    board_id: str,
    svc: BoardServiceDep,
    chat_svc: ChatServiceDep,
    uow: UowDep,
    current_user: CurrentUserDep,
    column_id: str = Form(...),
    title: str = Form(...),
) -> HTMLResponse:
    await require_board_access(current_user, board_id, uow)
    if await uow.boards.get_board_id_for_column(column_id) != board_id:
        raise HTTPException(status_code=404, detail="column not found")
    card = await svc.create_card(column_id, title, uow, actor_id=current_user["id"])
    # Re-render the WHOLE board and swap #board-wrap so the new card lands at
    # its canonical position (bottom) and every column stays consistent.
    board = await svc.get_board(board_id, uow)
    if board is None:
        raise HTTPException(status_code=404, detail="board not found")
    # Announce the new card in chat (planner), so board changes are visible there.
    col_name = {c.id: c.name for c in board.columns}
    ann = await chat_svc.announce(
        DEFAULT_CHANNEL_ID,
        f"📋 New card in {col_name.get(column_id, column_id)}: \"{card.title}\" (by {current_user['name']})",
        uow,
    )
    await manager.broadcast(DEFAULT_CHANNEL_ID, ann.model_dump(mode="json"))
    return templates.TemplateResponse(request=request, name="board_fragment.html", context={"board": board})
