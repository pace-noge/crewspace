"""API: board router — board page + card creation (HTMX fragments)."""
from __future__ import annotations

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, Response

from ...domain.identifiers import DEFAULT_CHANNEL_ID, DEFAULT_BOARD_ID
from ...domain.ports import UnitOfWork
from ...dto.markdown import render_message_markdown
from ..connection import manager
from ..deps import BoardServiceDep, ChatServiceDep, CurrentUserDep, CurrentUserOptionalDep, UowDep, require_member_redirect
from ..rendering import templates
from ...application.services import BoardService, ChatService
from ...application.access import require_board_access

router = APIRouter(prefix="/boards", tags=["board"])


@router.get("/{board_id}", response_class=HTMLResponse)
async def board_page(
    request: Request, board_id: str, svc: BoardServiceDep, uow: UowDep, current_user: CurrentUserOptionalDep
) -> Response:
    redirect = require_member_redirect(current_user)
    if redirect is not None:
        return redirect
    assert current_user is not None
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
    uow: UowDep, current_user: CurrentUserOptionalDep,
) -> Response:
    """Standalone single-column fragment (debug / inspection)."""
    redirect = require_member_redirect(current_user)
    if redirect is not None:
        return redirect
    assert current_user is not None
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


@router.get("/{board_id}/cards/{card_id}", response_class=HTMLResponse)
async def card_detail_page(
    request: Request, board_id: str, card_id: str, svc: BoardServiceDep,
    uow: UowDep, current_user: CurrentUserOptionalDep,
) -> Response:
    redirect = require_member_redirect(current_user)
    if redirect is not None:
        return redirect
    assert current_user is not None
    await _require_card_in_board(uow, board_id, card_id)
    await require_board_access(current_user, board_id, uow)
    detail = await svc.get_card_detail(card_id, uow)
    if detail is None:
        raise HTTPException(status_code=404, detail="card not found")
    members = await uow.auth.list_members()
    return templates.TemplateResponse(
        request=request,
        name="card_detail.html",
        context={
            "detail": detail,
            "board_id": board_id,
            "members": members,
            "current_user": current_user,
            "rendered_description": render_message_markdown(detail.card.description or "")
            if detail.card.description
            else None,
        },
    )


@router.post("/{board_id}/cards/{card_id}", response_class=HTMLResponse)
async def update_card_detail(
    request: Request, board_id: str, card_id: str, svc: BoardServiceDep,
    uow: UowDep, current_user: CurrentUserDep,
    title: str = Form(...),
    description: str = Form(""),
    assignee_id: str = Form(""),
    due_date: str = Form(""),
    priority: str = Form(""),
    labels: str = Form(""),
) -> Response:
    await _require_card_in_board(uow, board_id, card_id)
    await require_board_access(current_user, board_id, uow)
    if priority and priority not in {"low", "medium", "high", "urgent"}:
        raise HTTPException(status_code=422, detail="invalid priority")
    if assignee_id:
        member = await uow.auth.get_member(assignee_id)
        if member is None:
            raise HTTPException(status_code=422, detail="unknown assignee")
    parsed_labels = [x.strip() for x in labels.split(",") if x.strip()] if labels else []
    updated = await svc.update_card(
        card_id, uow,
        actor_id=current_user["id"],
        title=title,
        description=description,
        due_date=due_date,
        priority=priority,
        labels=parsed_labels,
    )
    await svc.set_assignee(card_id, assignee_id or None, uow, actor_id=current_user["id"])
    if updated is None:
        raise HTTPException(status_code=404, detail="card not found")
    detail = await svc.get_card_detail(card_id, uow)
    assert detail is not None
    members = await uow.auth.list_members()
    return templates.TemplateResponse(
        request=request,
        name="card_detail.html",
        context={
            "detail": detail,
            "board_id": board_id,
            "members": members,
            "current_user": current_user,
            "rendered_description": render_message_markdown(detail.card.description or "")
            if detail.card.description
            else None,
        },
    )


async def _require_card_in_board(uow: UnitOfWork, board_id: str, card_id: str) -> None:
    """404 unless the card exists and belongs to the given board (fail closed)."""
    board_of_card = await uow.boards.get_board_id_for_card(card_id)
    if board_of_card != board_id:
        raise HTTPException(status_code=404, detail="card not found")
