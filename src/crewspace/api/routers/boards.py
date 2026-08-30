"""API: board router — board page + card creation (HTMX fragments)."""
from __future__ import annotations

from fastapi import APIRouter, Depends, Form, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, Response

from ...domain.identifiers import DEFAULT_CHANNEL_ID, DEFAULT_BOARD_ID
from ...domain.ports import UnitOfWork
from ...dto.markdown import render_message_markdown
from ...dto.board import BoardDeltaDTO, card_run_badges
from ..board_live import board_room
from ..connection import manager
from ..deps import BoardServiceDep, ChatServiceDep, CurrentUserDep, CurrentUserOptionalDep, UowDep, require_member_redirect
from ..rendering import navigation_context, templates
from ...application.services import BoardService, ChatService
from ...application.column_triggers import board_workflow_badges
from ...application.access import require_board_access, can_access_board, can_access_workspace, can_manage_archived_board
from fastapi.responses import RedirectResponse

router = APIRouter(prefix="/boards", tags=["board"])


@router.websocket("/{board_id}/ws")
async def board_ws(websocket: WebSocket, board_id: str) -> None:
    """Authorized, receive-only subscription for board delta frames."""
    from ...security import is_same_origin, unsign_session
    from ..deps import SESSION_COOKIE

    if not is_same_origin(websocket.headers.get("origin"), str(websocket.url)):
        await websocket.close(code=4003)
        return
    db = websocket.app.state.db
    token = websocket.cookies.get(SESSION_COOKIE)
    sid = unsign_session(token, websocket.app.state.settings.secret) if token else None
    async with db.uow() as uow:
        member = await uow.auth.get_session_member(sid) if sid else None
        if member is None or not await can_access_board(member, board_id, uow):
            await websocket.close(code=4003)
            return

    room = board_room(board_id)
    await manager.connect(room, websocket)
    try:
        while True:
            # Keep the socket alive and ignore client frames: mutations flow
            # through authenticated HTTP endpoints, never through this socket.
            await websocket.receive_json()
    except WebSocketDisconnect:
        pass
    finally:
        manager.disconnect(room, websocket)


# Static GET routes MUST be registered before the dynamic GET /{board_id}
# below, or FastAPI matches board_id="new"/"settings" first and 404s.
@router.get("/new", response_class=HTMLResponse)
async def new_board_page(
    request: Request, uow: UowDep, current_user: CurrentUserOptionalDep
) -> Response:
    """Dedicated app-shell form to create a board in a chosen workspace."""
    redirect = require_member_redirect(current_user)
    if redirect is not None:
        return redirect
    assert current_user is not None
    if current_user["role"] == "superadmin":
        workspaces = []
        for team in await uow.teams.list_teams():
            workspaces.extend(await uow.workspaces.list_workspaces_for_team(team.id))
    else:
        workspaces = await uow.workspaces.list_workspaces_for_member(current_user["id"])
    return templates.TemplateResponse(
        request=request,
        name="board_new.html",
        context={
            "workspaces": workspaces,
            "current_user": current_user,
            "agents": await uow.auth.list_members(kind="agent"),
            **await navigation_context(uow, current_user),
        },
    )


@router.get("/{board_id}/settings", response_class=HTMLResponse)
async def board_settings_page(
    request: Request,
    board_id: str,
    svc: BoardServiceDep,
    uow: UowDep,
    current_user: CurrentUserOptionalDep,
) -> Response:
    """Dedicated app-shell settings surface: rename/archive the board and
    add/rename/reorder/archive/restore its columns."""
    redirect = require_member_redirect(current_user)
    if redirect is not None:
        return redirect
    assert current_user is not None
    await require_board_access(current_user, board_id, uow)
    board = await svc.get_board(board_id, uow)
    if board is None:
        raise HTTPException(status_code=404, detail="board not found")
    columns = await uow.boards.list_columns_active(board_id)
    archived_columns = await uow.boards.list_columns_archived(board_id)
    # Column→workflow trigger config (authorization-scoped) + the workflows the
    # member can pick from (scoped to channels they can see).
    column_triggers = await svc.list_column_trigger_config(board_id, current_user, uow)
    member_channels = await uow.channels.list_channels_for_member(current_user["id"])
    available_workflows = await uow.workflows.list_for_channels(
        [c.id for c in member_channels]
    )
    return templates.TemplateResponse(
        request=request,
        name="board_settings.html",
        context={
            "board": board,
            "columns": columns,
            "archived_columns": archived_columns,
            "column_triggers": column_triggers,
            "available_workflows": available_workflows,
            "current_user": current_user,
            "agents": await uow.auth.list_members(kind="agent"),
            **await navigation_context(uow, current_user),
        },
    )


@router.post("/{board_id}/settings/columns/{column_id}/trigger", response_class=HTMLResponse)
async def board_column_trigger_save(
    request: Request,
    board_id: str,
    column_id: str,
    svc: BoardServiceDep,
    uow: UowDep,
    current_user: CurrentUserOptionalDep,
    workflow_id: str | None = Form(None),
    enabled: str | None = Form(None),
) -> Response:
    """Configure (upsert) or clear a column→workflow rule from board settings."""
    redirect = require_member_redirect(current_user)
    if redirect is not None:
        return redirect
    assert current_user is not None
    await require_board_access(current_user, board_id, uow)
    board = await uow.boards.get_board(board_id)
    if board is None:
        raise HTTPException(status_code=404, detail="board not found")
    try:
        await svc.set_column_trigger(
            board_id=board_id,
            column_id=column_id,
            workflow_id=workflow_id or None,
            enabled=(enabled is not None),
            user=current_user,
            uow=uow,
        )
        await uow.commit()
    except (PermissionError, ValueError, KeyError) as exc:
        raise HTTPException(status_code=403 if isinstance(exc, PermissionError) else 400, detail=str(exc))
    return RedirectResponse(
        url=f"/boards/{board_id}/settings",
        status_code=303,
        headers={"HX-Redirect": f"/boards/{board_id}/settings"},
    )


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
    # Live linked-run statuses per card (badges + deep links). Empty for a
    # board the caller cannot access (authorization-scoped).
    card_run_statuses = await svc.board_run_statuses(board_id, current_user, uow)
    card_run_badge_links = {
        card_id: [badge for status in statuses for badge in card_run_badges(status)]
        for card_id, statuses in card_run_statuses.items()
    }
    card_workflow_badge_links = await board_workflow_badges(uow, board_id)
    return templates.TemplateResponse(
        request=request,
        name="board.html",
        context={
            "board": board,
            "current_user": current_user,
            "agents": agents,
            "card_run_statuses": card_run_statuses,
            "card_run_badge_links": card_run_badge_links,
            "card_workflow_badge_links": card_workflow_badge_links,
            **await navigation_context(uow, current_user),
        },
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
    # Render a REAL board DTO so card.html's move dropdown sees live columns
    # (the fragment still shows only this one column).
    board = await svc.get_board(board_id, uow)
    if board is None:
        raise HTTPException(status_code=404, detail="board not found")
    return templates.TemplateResponse(
        request=request, name="column.html", context={"board_id": board_id, "board": board, "column": column}
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
    if await uow.boards.is_column_archived(column_id):
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
    # Live board viewers: publish the minimal delta (they update in place).
    card_html = templates.get_template("card.html").render(
        card=card, board_id=board_id, board=board
    )
    await manager.broadcast(
        board_room(board_id),
        {
            "type": "board_delta",
            "board_id": board_id,
            "delta": BoardDeltaDTO(
                kind="card_created",
                card_id=card.id,
                title=card.title,
                to_column_id=column_id,
                card_html=card_html,
            ).model_dump(mode="json"),
        },
    )
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
    # Live board viewers: broadcast a card_updated delta so other clients
    # re-render this card in place (they keep their own scroll/state).
    updated_board = await svc.get_board(board_id, uow)
    if updated_board is not None:
        card_html = templates.get_template("card.html").render(
            card=detail.card, board_id=board_id, board=updated_board
        )
        await manager.broadcast(
            board_room(board_id),
            {
                "type": "board_delta",
                "board_id": board_id,
                "delta": BoardDeltaDTO(
                    kind="card_updated",
                    card_id=card_id,
                    title=detail.card.title,
                    card_html=card_html,
                ).model_dump(mode="json"),
            },
        )
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


# ---------------------------------------------------------------------------
# M7.2 — board & column management (HTMX/forms + board switcher)
# ---------------------------------------------------------------------------


@router.post("", response_class=HTMLResponse)
async def create_board(
    request: Request,
    svc: BoardServiceDep,
    uow: UowDep,
    current_user: CurrentUserDep,
    workspace_id: str = Form(...),
    name: str = Form(...),
) -> Response:
    await require_board_access_for_workspace(current_user, workspace_id, uow)
    created = await svc.create_board(workspace_id, name, uow)
    return RedirectResponse(f"/board/{created.id}", status_code=303)

@router.post("/{board_id}/rename", response_class=HTMLResponse)
async def rename_board(
    request: Request,
    board_id: str,
    svc: BoardServiceDep,
    uow: UowDep,
    current_user: CurrentUserDep,
    name: str = Form(...),
) -> Response:
    await require_board_access(current_user, board_id, uow)
    await svc.rename_board(board_id, name, uow)
    return RedirectResponse(f"/board/{board_id}", status_code=303)


@router.post("/{board_id}/archive", response_class=HTMLResponse)
async def archive_board(
    request: Request,
    board_id: str,
    svc: BoardServiceDep,
    uow: UowDep,
    current_user: CurrentUserDep,
) -> Response:
    await require_board_access(current_user, board_id, uow)
    await svc.archive_board(board_id, uow)
    return RedirectResponse("/board", status_code=303)


@router.post("/{board_id}/restore", response_class=HTMLResponse)
async def restore_board(
    request: Request,
    board_id: str,
    svc: BoardServiceDep,
    uow: UowDep,
    current_user: CurrentUserDep,
) -> Response:
    if not await can_manage_archived_board(current_user, board_id, uow):
        raise HTTPException(status_code=404, detail="board not found")
    await svc.restore_board(board_id, uow)
    return RedirectResponse(f"/board/{board_id}", status_code=303)


@router.post("/{board_id}/columns", response_class=HTMLResponse)
async def create_column(
    request: Request,
    board_id: str,
    svc: BoardServiceDep,
    uow: UowDep,
    current_user: CurrentUserDep,
    name: str = Form(...),
) -> Response:
    await require_board_access(current_user, board_id, uow)
    await svc.create_column(board_id, name, uow)
    return RedirectResponse(f"/board/{board_id}", status_code=303)


@router.post("/{board_id}/columns/{column_id}/rename", response_class=HTMLResponse)
async def rename_column(
    request: Request,
    board_id: str,
    column_id: str,
    svc: BoardServiceDep,
    uow: UowDep,
    current_user: CurrentUserDep,
    name: str = Form(...),
) -> Response:
    await require_board_access(current_user, board_id, uow)
    if await uow.boards.get_board_id_for_column(column_id) != board_id:
        raise HTTPException(status_code=404, detail="column not found")
    await svc.rename_column(column_id, name, uow)
    return RedirectResponse(f"/board/{board_id}", status_code=303)


@router.post("/{board_id}/columns/{column_id}/reorder", response_class=HTMLResponse)
async def reorder_column(
    request: Request,
    board_id: str,
    column_id: str,
    svc: BoardServiceDep,
    uow: UowDep,
    current_user: CurrentUserDep,
    before_column_id: str | None = Form(None),
) -> Response:
    await require_board_access(current_user, board_id, uow)
    if await uow.boards.get_board_id_for_column(column_id) != board_id:
        raise HTTPException(status_code=404, detail="column not found")
    if before_column_id:
        # Fail closed: the reorder target must be an ACTIVE sibling of the SAME
        # board; a missing/archived/foreign id must never silently mean
        # "move to end".
        target_board = await uow.boards.get_board_id_for_column(before_column_id)
        if target_board != board_id:
            raise HTTPException(status_code=404, detail="reorder target not found")
        target_is_archived = await uow.boards.is_column_archived(before_column_id)
        if target_is_archived:
            raise HTTPException(status_code=404, detail="reorder target not found")
    await svc.reorder_column(column_id, uow, before_column_id)
    return RedirectResponse(f"/board/{board_id}", status_code=303)


@router.post("/{board_id}/columns/{column_id}/archive", response_class=HTMLResponse)
async def archive_column(
    request: Request,
    board_id: str,
    column_id: str,
    svc: BoardServiceDep,
    uow: UowDep,
    current_user: CurrentUserDep,
) -> Response:
    await require_board_access(current_user, board_id, uow)
    if await uow.boards.get_board_id_for_column(column_id) != board_id:
        raise HTTPException(status_code=404, detail="column not found")
    await svc.archive_column(column_id, uow)
    return RedirectResponse(f"/board/{board_id}", status_code=303)


@router.post("/{board_id}/columns/{column_id}/restore", response_class=HTMLResponse)
async def restore_column(
    request: Request,
    board_id: str,
    column_id: str,
    svc: BoardServiceDep,
    uow: UowDep,
    current_user: CurrentUserDep,
) -> Response:
    await require_board_access(current_user, board_id, uow)
    if await uow.boards.get_board_id_for_column(column_id) != board_id:
        raise HTTPException(status_code=404, detail="column not found")
    await svc.restore_column(column_id, uow)
    return RedirectResponse(f"/board/{board_id}", status_code=303)


async def require_board_access_for_workspace(user: dict, workspace_id: str, uow: UnitOfWork) -> None:
    """404 unless the principal may act within the workspace (fail closed)."""
    if not await can_access_workspace(user, workspace_id, uow):
        raise HTTPException(status_code=404, detail="workspace not found")
