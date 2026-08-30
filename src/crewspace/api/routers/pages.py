"""API: page router — top-level HTML pages + health."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from ..deps import BoardServiceDep, CurrentUserDep, CurrentUserOptionalDep, UowDep, require_member_redirect
from ...domain.identifiers import DEFAULT_BOARD_ID, DEFAULT_CHANNEL_ID
from ..rendering import navigation_context, templates
from ...application.access import can_access_board, can_manage_archived_board

router = APIRouter(tags=["pages"])


@router.get("/direct/{peer_id}")
async def open_direct_message(
    peer_id: str, uow: UowDep, current_user: CurrentUserOptionalDep
) -> RedirectResponse:
    redirect = require_member_redirect(current_user)
    if redirect is not None:
        return redirect
    assert current_user is not None
    peer = await uow.auth.get_member(peer_id)
    if peer is None or peer_id == current_user["id"]:
        return RedirectResponse("/", status_code=303)
    channel = await uow.channels.get_or_create_direct(current_user["id"], peer_id)
    await uow.commit()
    return RedirectResponse(f"/channels/{channel.id}", status_code=303)


@router.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@router.get("/", response_class=HTMLResponse)
async def home(request: Request, uow: UowDep, current_user: CurrentUserOptionalDep) -> Response:
    redirect = require_member_redirect(current_user)
    if redirect is not None:
        return redirect
    assert current_user is not None
    agents = await uow.auth.list_members(kind="agent")
    channel = await uow.channels.get_channel(DEFAULT_CHANNEL_ID)
    if channel is None or not await uow.channels.can_member_access(
        DEFAULT_CHANNEL_ID, current_user["id"]
    ):
        return HTMLResponse("<h1>No active channel</h1>", status_code=404)
    workspace = await uow.workspaces.get_workspace(channel.workspace_id)
    channel_members = await uow.channels.list_members(DEFAULT_CHANNEL_ID)
    context = {
        "channel_id": DEFAULT_CHANNEL_ID,
        "channel": channel,
        "workspace": workspace,
        "current_user": current_user,
        "agents": agents,
        "channel_member_count": len(channel_members),
        **await navigation_context(uow, current_user),
    }
    return templates.TemplateResponse(
        request=request, name="chat.html", context=context
    )


@router.get("/channels/{channel_id}", response_class=HTMLResponse)
async def channel_page(
    request: Request, channel_id: str, uow: UowDep, current_user: CurrentUserOptionalDep
) -> Response:
    redirect = require_member_redirect(current_user)
    if redirect is not None:
        return redirect
    assert current_user is not None
    if not await uow.channels.can_member_access(channel_id, current_user["id"]):
        return HTMLResponse("<h1>Channel not found</h1>", status_code=404)
    channel = await uow.channels.get_channel(channel_id)
    if channel is None:
        return HTMLResponse("<h1>Channel not found</h1>", status_code=404)
    workspace = await uow.workspaces.get_workspace(channel.workspace_id)
    channel_members = await uow.channels.list_members(channel_id)
    direct_peer = await uow.channels.get_direct_peer(channel_id, current_user["id"])
    return templates.TemplateResponse(
        request=request,
        name="chat.html",
        context={
            "channel_id": channel_id,
            "channel": channel,
            "workspace": workspace,
            "current_user": current_user,
            "agents": await uow.auth.list_members(kind="agent"),
            "channel_member_count": len(channel_members),
            "direct_peer": direct_peer,
            **await navigation_context(uow, current_user),
        },
    )


@router.get("/board", response_class=HTMLResponse)
async def board_list(request: Request, uow: UowDep, current_user: CurrentUserOptionalDep) -> Response:
    """Board index: lists the user's live boards and lets them restore
    archived ones. Also the redirect target after archiving a board."""
    redirect = require_member_redirect(current_user)
    if redirect is not None:
        return redirect
    assert current_user is not None
    from ..rendering import _boards_menu

    boards = await _boards_menu(uow, current_user)
    return templates.TemplateResponse(
        request=request,
        name="board_index.html",
        context={
            "boards": boards,
            "current_user": current_user,
            "agents": await uow.auth.list_members(kind="agent"),
            **await navigation_context(uow, current_user),
        },
    )


@router.get("/board/{board_id}", response_class=HTMLResponse)
async def board_page(request: Request, board_id: str, svc: BoardServiceDep, uow: UowDep, current_user: CurrentUserOptionalDep) -> Response:
    redirect = require_member_redirect(current_user)
    if redirect is not None:
        return redirect
    assert current_user is not None
    # Authorized workspace member viewing an archived board: recoverable but
    # hidden from the default view — send them to the board index.
    if await can_manage_archived_board(current_user, board_id, uow):
        board = await svc.get_board(board_id, uow)
        if board is not None and board.archived_at is not None:
            return RedirectResponse("/board", status_code=303)
    if not await can_access_board(current_user, board_id, uow):
        # A board the user cannot access (unknown, foreign, or archived-and-
        # outside-their-workspace) is indistinguishable from a 404.
        raise HTTPException(status_code=404, detail="board not found")
    board = await svc.get_board(board_id, uow)
    if board is None:
        return HTMLResponse("<h1>Board not found</h1>", status_code=404)
    agents = await uow.auth.list_members(kind="agent")
    context = {
        "board": board,
        "current_user": current_user,
        "agents": agents,
        **await navigation_context(uow, current_user),
    }
    return templates.TemplateResponse(
        request=request, name="board.html", context=context
    )
