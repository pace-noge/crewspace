"""API: WebSocket connection managers.

Two independent broadcast spaces:

  * ``manager`` (ConnectionManager) — human/chat clients per channel. Agent
    replies flow through here too, so the UI needs no special-casing.
  * ``agent_manager`` (AgentConnectionManager) — connected *agent* processes.
    Agents are separate programs on their own machines that dial INTO the app
    over WebSocket (Buzz-style: the agent connects, not the app). The app
    pushes events down to a connected agent and reads its replies/tool calls
    back over that same socket. For multi-worker, back both with Redis pub/sub.
"""

from __future__ import annotations

import asyncio
import secrets
from typing import Any

from fastapi import WebSocket


class ConnectionManager:
    def __init__(self) -> None:
        self._rooms: dict[str, set[WebSocket]] = {}

    async def connect(self, channel_id: str, ws: WebSocket) -> None:
        await ws.accept()
        self._rooms.setdefault(channel_id, set()).add(ws)

    def disconnect(self, channel_id: str, ws: WebSocket) -> None:
        room = self._rooms.get(channel_id)
        if room and ws in room:
            room.discard(ws)
            if not room:
                self._rooms.pop(channel_id, None)

    def reset(self) -> None:
        """Drop socket bookkeeping when the owning application shuts down."""
        self._rooms.clear()

    async def broadcast(self, channel_id: str, payload: dict) -> None:
        for ws in list(self._rooms.get(channel_id, set())):
            try:
                await ws.send_json(payload)
            except Exception:
                self.disconnect(channel_id, ws)


class AgentConnectionManager:
    """Tracks live agent WebSocket connections and lets callers talk to them.

    Routing is by agent id. ``send_and_wait`` pushes a frame to an agent and
    resolves when the agent sends a correlated reply (used for chat mentions).
    """

    def __init__(self) -> None:
        self._conns: dict[str, WebSocket] = {}
        self._waiters: dict[tuple[str, str], asyncio.Future[Any]] = {}

    async def connect(self, agent_id: str, ws: WebSocket) -> None:
        await ws.accept()
        self._conns[agent_id] = ws
        _broadcast_presence(agent_id, "connected")

    def disconnect(self, agent_id: str, ws: WebSocket) -> None:
        if self._conns.get(agent_id) is ws:
            self._conns.pop(agent_id, None)
            _broadcast_presence(agent_id, "disconnected")

    def reset(self) -> None:
        """Drop live connections and cancel unresolved waits on app shutdown."""
        self._conns.clear()
        for future in self._waiters.values():
            if not future.done():
                future.cancel()
        self._waiters.clear()

    async def close(self, agent_id: str, code: int = 4004) -> None:
        ws = self._conns.pop(agent_id, None)
        if ws is not None:
            await ws.close(code=code)

    def is_connected(self, agent_id: str) -> bool:
        return agent_id in self._conns

    def status(self, agent_id: str, *, is_local: bool = False) -> str:
        """Return ``connected``, ``local``, or ``disconnected`` for the UI.

        Local/builtin agents have no public key and run in the main process.
        Remote agents have a public key and are connected only while their live
        WebSocket is present.
        """
        if self.is_connected(agent_id):
            return "connected"
        return "local" if is_local else "disconnected"

    def connected_agent_ids(self) -> set[str]:
        return set(self._conns)

    async def send(self, agent_id: str, payload: dict) -> None:
        ws = self._conns.get(agent_id)
        if ws is None:
            raise KeyError(f"agent {agent_id} not connected")
        await ws.send_json(payload)

    def new_message_id(self) -> str:
        return secrets.token_urlsafe(18)

    def _resolve(self, agent_id: str, message_id: str, value: Any) -> bool:
        fut = self._waiters.pop((agent_id, message_id), None)
        if fut is not None and not fut.done():
            fut.set_result(value)
            return True
        return False

    async def send_and_wait(self, agent_id: str, payload: dict, timeout: float = 20.0) -> Any:
        """Send a frame to a connected agent and await its correlated reply."""
        if agent_id not in self._conns:
            raise KeyError(f"agent {agent_id} not connected")
        mid = payload.get("message_id") or self.new_message_id()
        payload = {**payload, "message_id": mid}
        loop = asyncio.get_event_loop()
        fut: asyncio.Future[Any] = loop.create_future()
        waiter_key = (agent_id, mid)
        self._waiters[waiter_key] = fut
        try:
            await self.send(agent_id, payload)
            return await asyncio.wait_for(fut, timeout=timeout)
        finally:
            self._waiters.pop(waiter_key, None)

    def deliver_reply(self, agent_id: str, message_id: str, value: Any) -> bool:
        """Called by the agent WS loop when an agent sends a correlated reply."""
        return self._resolve(agent_id, message_id, value)


def _broadcast_presence(agent_id: str, status: str) -> None:
    """Notify every open UI client that an agent came online or dropped.

    Sent on a dedicated presence channel (not the per-channel chat rooms) so
    the sidebar status dots can update live without a page reload. Fire-and-forget
    on the running event loop: ``connect`` is async and ``disconnect`` is sync,
    but both run inside the agent's live WebSocket loop.
    """
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    loop.create_task(
        manager.broadcast(
            PRESENCE_ROOM,
            {"type": "agent_presence", "agent_id": agent_id, "status": status},
        )
    )


manager = ConnectionManager()
agent_manager = AgentConnectionManager()
thread_manager = ConnectionManager()  # per-thread side-panel WebSockets

#: Broadcast channel that carries global agent-presence events to every open
#: UI client (the sidebar status dots subscribe here so they update live
#: without a page reload).
PRESENCE_ROOM = "__presence__"
