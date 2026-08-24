"""API: chat router — REST history + WebSocket broadcast.

The WebSocket handler is intentionally small: it validates input then delegates
to `ChatService.post_and_respond`, which owns the agent interaction. The route
stays a thin transport; business logic lives in the application layer.
"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, WebSocket, WebSocketDisconnect
from pydantic import BaseModel

from ...application.workspace_service import WorkspaceService
from ...application.workflows import WorkflowService
from ...dto.mappers import to_message
from ...infrastructure.mcp_client import ExternalMcpToolExecutor
from ...infrastructure.workflow_webhooks import build_workflow_webhook_executor
from ..connection import manager
from ..deps import ChatServiceDep, CurrentUserDep, UowDep, WorkspaceServiceDep

router = APIRouter(prefix="/channels", tags=["chat"])


async def _broadcast_workflow_message(message) -> None:
    await manager.broadcast(
        message.channel_id, to_message(message).model_dump(mode="json")
    )


async def _broadcast_workflow_progress(event: dict) -> None:
    channel_id = event.get("channel_id")
    if channel_id:
        await manager.broadcast(channel_id, event)


def _workflow_service() -> WorkflowService:
    return WorkflowService(
        on_message=_broadcast_workflow_message,
        on_progress=_broadcast_workflow_progress,
        webhook_executor=build_workflow_webhook_executor(),
        mcp_executor=ExternalMcpToolExecutor(),
    )


class ReactionInput(BaseModel):
    emoji: str


class DiffInput(BaseModel):
    text: str


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
    await _workflow_service().dispatch(
        uow, channel_id=channel_id, trigger_type="reaction_added",
        event={"message_id": message_id, "channel_id": channel_id,
               "member_id": current_user["id"], "emoji": emoji, "text": emoji},
    )
    return reactions


@router.post("/{channel_id}/diffs", status_code=201)
async def post_diff(
    channel_id: str, payload: DiffInput, current_user: CurrentUserDep, uow: UowDep,
) -> dict:
    if not await uow.channels.can_member_access(channel_id, current_user["id"]):
        raise HTTPException(status_code=404, detail="Channel not found")
    text = payload.text.strip()
    if not text:
        raise HTTPException(status_code=422, detail="Diff is required")
    runs = await _workflow_service().dispatch(
        uow, channel_id=channel_id, trigger_type="diff_posted",
        event={"channel_id": channel_id, "author_id": current_user["id"], "text": text},
    )
    return {"run_ids": [run.id for run in runs]}


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
                async def on_human_persisted(human_dto):
                    # Echo the human message before running workflows, then stream
                    # in-process workflow progress and generated messages directly.
                    await manager.broadcast(channel_id, human_dto.model_dump(mode="json"))
                    try:
                        await WorkflowService(
                            on_message=_broadcast_workflow_message,
                            on_progress=_broadcast_workflow_progress,
                            webhook_executor=build_workflow_webhook_executor(),
                            mcp_executor=ExternalMcpToolExecutor(),
                        ).dispatch(
                            uow,
                            channel_id=channel_id,
                            trigger_type="message_posted",
                            event={
                                "message_id": human_dto.id,
                                "channel_id": channel_id,
                                "author_id": member_id,
                                "text": body,
                                "thread_id": thread_id,
                            },
                        )
                    except PermissionError:
                        pass

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
                    # When a connected remote agent is engaged over its socket,
                    # signal that it is actively working (cleared when its reply
                    # frame arrives). Mirrors the in-process workflow progress.
                    on_agent_progress=lambda aid: manager.broadcast(
                        channel_id,
                        {"type": "agent_working", "author_id": aid, "channel_id": channel_id},
                    ),
                    on_agent_output=lambda aid, mid, text: manager.broadcast(
                        channel_id,
                        {
                            "type": "agent_progress",
                            "author_id": aid,
                            "channel_id": channel_id,
                            "message_id": mid,
                            "text": text,
                        },
                    ),
                    on_agent_output_complete=lambda aid, mid: manager.broadcast(
                        channel_id,
                        {
                            "type": "agent_progress_complete",
                            "author_id": aid,
                            "channel_id": channel_id,
                            "message_id": mid,
                        },
                    ),
                    on_human_persisted=on_human_persisted,
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
