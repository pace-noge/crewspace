"""API: chat router — REST history + WebSocket broadcast.

The WebSocket handler is intentionally small: it validates input then delegates
to `ChatService.post_and_respond`, which owns the agent interaction. The route
stays a thin transport; business logic lives in the application layer.
"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, WebSocket, WebSocketDisconnect
from pydantic import BaseModel

from ...application.workspace_service import WorkspaceService
from ..connection import manager
from ..deps import ChatServiceDep, CurrentUserDep, UowDep, WorkspaceServiceDep

router = APIRouter(prefix="/channels", tags=["chat"])


class ReactionInput(BaseModel):
    emoji: str


@router.get("/{channel_id}/messages")
async def messages(
    channel_id: str,
    svc: ChatServiceDep,
    ws_svc: WorkspaceServiceDep,
    uow: UowDep,
    current_user: CurrentUserDep,
) -> list:
    if not await uow.channels.can_member_access(channel_id, current_user["id"]):
        raise HTTPException(status_code=404, detail="Channel not found")
    dtos = await svc.list_messages(channel_id, uow)
    return [d.model_dump(mode="json") for d in dtos]


@router.get("/{channel_id}/threads/{thread_id}")
async def thread_messages(
    channel_id: str, thread_id: str, svc: ChatServiceDep, uow: UowDep,
    current_user: CurrentUserDep,
) -> list:
    if not await uow.channels.can_member_access(channel_id, current_user["id"]):
        raise HTTPException(status_code=404, detail="Thread not found")
    dtos = await svc.list_thread(thread_id, uow)
    if not dtos or dtos[0].channel_id != channel_id:
        raise HTTPException(status_code=404, detail="Thread not found")
    return [dto.model_dump(mode="json") for dto in dtos]


@router.get("/{channel_id}/messages/{message_id}/reactions")
async def message_reactions(
    channel_id: str, message_id: str, current_user: CurrentUserDep, uow: UowDep,
) -> list[dict]:
    if not await uow.channels.can_member_access(channel_id, current_user["id"]):
        raise HTTPException(status_code=404, detail="Message not found")
    message = await uow.chat.list_thread(message_id)
    if not message or message[0].channel_id != channel_id:
        raise HTTPException(status_code=404, detail="Message not found")
    return await uow.chat.list_reactions(message_id, current_user["id"])


@router.post("/{channel_id}/messages/{message_id}/reactions")
async def toggle_message_reaction(
    channel_id: str, message_id: str, payload: ReactionInput,
    current_user: CurrentUserDep, uow: UowDep,
) -> list[dict]:
    if not await uow.channels.can_member_access(channel_id, current_user["id"]):
        raise HTTPException(status_code=404, detail="Message not found")
    emoji = payload.emoji.strip()
    if not emoji or len(emoji) > 16:
        raise HTTPException(status_code=422, detail="Invalid emoji")
    message = await uow.chat.list_thread(message_id)
    if not message or message[0].channel_id != channel_id:
        raise HTTPException(status_code=404, detail="Message not found")
    reactions = await uow.chat.toggle_reaction(message_id, current_user["id"], emoji)
    await uow.commit()
    return reactions


@router.websocket("/{channel_id}/ws")
async def chat_ws(
    websocket: WebSocket,
    channel_id: str,
    svc: ChatServiceDep,
    ws_svc: WorkspaceServiceDep,
) -> None:
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
        member_id = member["id"]
        if not await uow.channels.can_member_access(channel_id, member_id):
            await websocket.close(code=4003)
            return
        direct_peer = await uow.channels.get_direct_peer(channel_id, member_id)

    await manager.connect(channel_id, websocket)
    try:
        while True:
            raw = await websocket.receive_json()
            body = (raw.get("body") or "").strip()
            if not body:
                continue
            thread_id = (raw.get("thread_id") or "").strip() or None
            async with db.uow() as uow:
                if thread_id:
                    thread = await svc.list_thread(thread_id, uow)
                    if not thread or thread[0].channel_id != channel_id:
                        continue
                new_msgs = await svc.post_and_respond(
                    channel_id,
                    member_id,
                    body,
                    uow,
                    thread_id=thread_id,
                    routing_text=(
                        f"@{direct_peer['name']} {body}"
                        if direct_peer is not None and direct_peer["kind"] == "agent"
                        else None
                    ),
                    # Broadcast a typing indicator the moment we know which agent
                    # will answer (before the possibly-slow agent call runs).
                    on_agent_resolved=lambda aid: manager.broadcast(
                        channel_id,
                        {"type": "typing", "author_id": aid, "channel_id": channel_id},
                    ),
                    on_human_persisted=lambda human_dto: manager.broadcast(
                        channel_id, human_dto.model_dump(mode="json")
                    ),
                )
            for m in new_msgs:
                # The human message was already broadcast immediately on
                # persist (via on_human_persisted); skip it here to avoid a
                # duplicate frame. Agent replies are always broadcast.
                if m is new_msgs[0] and m.author_kind != "agent":
                    continue
                await manager.broadcast(channel_id, m.model_dump(mode="json"))
    except WebSocketDisconnect:
        pass
    finally:
        manager.disconnect(channel_id, websocket)
