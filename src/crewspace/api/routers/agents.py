"""API: agent WebSocket — the inbound connection remote agents dial into.

A remote agent process (on its own machine) connects here. It proves identity
with an Ed25519-signed connect claim sent as ``Authorization: Bearer <token>``
(Buzz-style: each agent owns a keypair; the public key is registered with the
member, the private key lives only in the agent). The server verifies the
signature against the stored public key and that the claim is fresh.

After connect, the app pushes events DOWN to the agent (a chat message that
@mentions it, or a board event). The agent replies over the same socket and
**signs every action** (``sig`` field); the server re-verifies before applying
it, giving a non-repudiable audit trail. The authenticated agent id is forced as
the actor on every tool call, so an agent can only ever act AS ITSELF.
"""
from __future__ import annotations

import json

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from ..connection import agent_manager
from ...application.change_sets import ChangeSetService
from ...application.tools import build_registry
from ...config import get_settings
from ...security import verify_connect_claim

router = APIRouter(prefix="/agents", tags=["agents"])


@router.websocket("/ws")
async def agent_ws(websocket: WebSocket):
    settings = get_settings()
    auth = websocket.headers.get("authorization", "")
    if not auth.lower().startswith("bearer "):
        await websocket.close(code=4001)
        return
    token = auth[7:].strip()

    # Resolve + verify the agent's identity from the signed claim.
    # We don't know the agent id until we parse the claim, so first decode it to
    # get the claimed id, then fetch that member's public key and verify.
    claimed_id = _claimed_id(token)
    pubkey = await _pubkey_for(websocket, claimed_id)
    if pubkey is None or (agent_id := verify_connect_claim(token, pubkey)) is None:
        await websocket.close(code=4001)
        return

    await agent_manager.connect(agent_id, websocket)
    registry = build_registry()
    try:
        while True:
            frame = await websocket.receive_json()
            ftype = frame.get("type")
            if not agent_manager.is_active_socket(agent_id, websocket):
                await websocket.send_json(
                    {"type": "error", "error": "stale connection"}
                )
                continue
            # Every action must be signed by the agent; verify before applying.
            if not _verify_frame(pubkey, frame):
                await websocket.send_json({"type": "error", "error": "bad signature"})
                continue
            if ftype != "hello" and not agent_manager.validate_frame_sequence(
                agent_id, websocket, frame
            ):
                await websocket.send_json(
                    {"type": "error", "error": "invalid or replayed sequence"}
                )
                continue
            if ftype == "reply":
                agent_manager.deliver_reply(
                    agent_id, frame.get("message_id", ""), frame.get("text", "")
                )
            elif ftype == "hello":
                try:
                    profile = agent_manager.negotiate_capabilities(
                        agent_id, websocket, frame
                    )
                except ValueError as exc:
                    await websocket.send_json({"type": "error", "error": str(exc)})
                    continue
                await websocket.send_json(
                    {
                        "type": "hello_ack",
                        "protocol_version": profile["protocol_version"],
                        "capabilities": profile["capabilities"],
                        "max_concurrency": profile["max_concurrency"],
                        "session_id": profile["session_id"],
                    }
                )
            elif ftype == "agent_progress":
                if not agent_manager.supports(agent_id, "progress"):
                    await websocket.send_json(
                        {"type": "error", "error": "unsupported capability: progress"}
                    )
                    continue
                text = frame.get("text", "")
                if not isinstance(text, str) or not text or len(text) > 16_384:
                    await websocket.send_json(
                        {"type": "error", "error": "invalid agent progress"}
                    )
                    continue
                await agent_manager.deliver_progress(
                    agent_id, frame.get("message_id", ""), text
                )
            elif ftype == "tool":
                if not agent_manager.supports(agent_id, "tools"):
                    await websocket.send_json(
                        {"type": "error", "error": "unsupported capability: tools"}
                    )
                    continue
                await _run_tool(websocket, registry, agent_id, frame)
            elif ftype == "agent_activity":
                profile = agent_manager.capability_profile(agent_id)
                if profile is None or profile["legacy"]:
                    await websocket.send_json(
                        {
                            "type": "error",
                            "error": "unsupported capability: agent_activity",
                        }
                    )
                    continue
                try:
                    profile = agent_manager.update_activity(
                        agent_id, websocket, frame.get("active_runs")
                    )
                except ValueError as exc:
                    await websocket.send_json({"type": "error", "error": str(exc)})
                    continue
                await websocket.send_json(
                    {
                        "type": "agent_activity_ack",
                        "active_runs": profile["active_runs"],
                        "max_concurrency": profile["max_concurrency"],
                    }
                )
            elif ftype == "coding_change_set":
                if not agent_manager.supports(agent_id, "coding_workspace"):
                    await websocket.send_json(
                        {
                            "type": "error",
                            "error": "unsupported capability: coding_workspace",
                        }
                    )
                    continue
                request_id = frame.get("request_id", "")
                error = await _handle_coding_change_set(
                    agent_manager,
                    websocket.app.state.db,
                    agent_id=agent_id,
                    request_id=request_id,
                    value=frame.get("change_set"),
                )
                if error:
                    await websocket.send_json({"type": "error", "error": error})
            elif ftype == "coding_run_failed":
                if not agent_manager.supports(agent_id, "coding_workspace"):
                    await websocket.send_json(
                        {"type": "error", "error": "unsupported capability: coding_workspace"}
                    )
                    continue
                agent_manager.deliver_coding_failure(
                    agent_id,
                    frame.get("request_id", ""),
                    frame.get("error"),
                )
            elif ftype in {
                "coding_workspace_action_result",
                "coding_workspace_action_failed",
            }:
                if not agent_manager.supports(agent_id, "coding_workspace"):
                    await websocket.send_json(
                        {"type": "error", "error": "unsupported capability: coding_workspace"}
                    )
                    continue
                _handle_workspace_action_frame(
                    agent_manager, agent_id, websocket, frame
                )
            # Unknown frame types are ignored.
    except WebSocketDisconnect:
        agent_manager.disconnect(agent_id, websocket)
    except Exception:
        agent_manager.disconnect(agent_id, websocket)


def _claimed_id(token: str) -> str | None:
    from ...security import _b64u_decode
    import base64 as _b

    try:
        body = token.split(".", 1)[0]
        pad = "=" * (-len(body) % 4)
        payload = json.loads(_b.urlsafe_b64decode(body + pad))
        return payload.get("agent_id")
    except Exception:
        return None


async def _pubkey_for(websocket: WebSocket, agent_id: str | None) -> str | None:
    if not agent_id:
        return None
    db = websocket.app.state.db
    async with db.uow() as uow:
        member = await uow.auth.get_member(agent_id)
        if not member or member["kind"] != "agent":
            return None
        return await uow.auth.get_pubkey(agent_id)


async def _persist_coding_change_set(
    db, *, agent_id: str, request_id: str, change_set
) -> None:
    async with db.uow() as uow:
        await ChangeSetService().record_capture(
            agent_id=agent_id,
            request_id=request_id,
            change_set=change_set,
            uow=uow,
        )
        await uow.commit()


async def _handle_coding_change_set(
    manager, db, *, agent_id: str, request_id: str, value
) -> str | None:
    try:
        change_set = manager.validate_coding_change_set(
            agent_id, request_id, value
        )
    except ValueError as exc:
        manager.deliver_coding_change_set(agent_id, request_id, value)
        return str(exc)
    if change_set is None:
        return None
    try:
        await _persist_coding_change_set(
            db,
            agent_id=agent_id,
            request_id=request_id,
            change_set=change_set,
        )
    except Exception:
        error = "change set persistence failed"
        manager.deliver_coding_failure(agent_id, request_id, error)
        return error
    manager.complete_coding_change_set(agent_id, request_id, change_set)
    return None


def _handle_workspace_action_frame(
    manager, agent_id: str, websocket: WebSocket, frame: dict
) -> bool:
    request_id = frame.get("request_id", "")
    if frame.get("type") == "coding_workspace_action_result":
        return manager.deliver_workspace_action_result(
            agent_id, request_id, frame.get("result"), websocket
        )
    if frame.get("type") == "coding_workspace_action_failed":
        return manager.deliver_workspace_action_failure(
            agent_id, request_id, frame.get("error"), websocket
        )
    return False


def _verify_frame(pubkey: str, frame: dict) -> bool:
    from ...security import verify_payload

    sig = frame.pop("sig", None)
    if not sig:
        return False
    ok = verify_payload(pubkey, frame, sig)
    # restore sig so the handler can still read it if needed
    frame["sig"] = sig
    return ok


async def _run_tool(websocket: WebSocket, registry, agent_id: str, frame: dict) -> None:
    name = frame.get("name", "")
    args = frame.get("args", {}) or {}
    db = websocket.app.state.db
    async with db.uow() as uow:
        try:
            for identity_field in ("author_id", "creator_id", "actor_id"):
                args.pop(identity_field, None)
            result = await registry.bind_trusted(
                uow, principal_id=agent_id, agent_id=agent_id
            ).run(name, **args)
            await uow.commit()
        except Exception as exc:
            result = {"error": f"{type(exc).__name__}: {exc}"}
    await websocket.send_json(
        {"type": "tool_result", "call_id": frame.get("call_id"), "result": result}
    )
