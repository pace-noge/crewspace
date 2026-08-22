"""API: global agent-presence WebSocket.

A single, authenticated socket per UI client that carries ``agent_presence``
events for every connected/disconnected remote agent, so the sidebar status
dots update live without a page reload. Distinct from the per-channel chat
rooms: presence is global, not scoped to one channel.
"""

from __future__ import annotations

from fastapi import APIRouter, WebSocket

from ..connection import PRESENCE_ROOM, manager

router = APIRouter(tags=["presence"])


@router.websocket("/ws/presence")
async def presence_ws(websocket: WebSocket) -> None:
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
        if member is None:
            await websocket.close(code=4001)
            return

    await manager.connect(PRESENCE_ROOM, websocket)
    try:
        # The presence socket is receive-only; keep it open until the client
        # disconnects so it keeps receiving connect/disconnect events.
        while True:
            await websocket.receive()
    except Exception:
        pass
    finally:
        manager.disconnect(PRESENCE_ROOM, websocket)
