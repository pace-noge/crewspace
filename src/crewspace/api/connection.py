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
import re
import secrets
from collections.abc import Awaitable, Callable
from typing import Any

from fastapi import WebSocket


AGENT_PROTOCOL_VERSION = 1
_SAFE_CODING_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}\Z")
AGENT_CAPABILITIES = frozenset(
    {
        "progress", "cancellation", "tools", "artifacts", "patches", "resume",
        "heartbeat", "coding_workspace",
    }
)
LEGACY_AGENT_PROFILE: dict[str, Any] = {
    "protocol_version": 0,
    "agent_version": "legacy",
    # Existing agents already use progress and tool frames, so their explicit
    # compatibility profile preserves only those established behaviors.
    "capabilities": ["progress", "tools"],
    "max_concurrency": 1,
    "active_runs": 0,
    "legacy": True,
}


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
        self._waiter_sockets: dict[tuple[str, str], WebSocket] = {}
        self._progress_handlers: dict[
            tuple[str, str], Callable[[str, str], Awaitable[None]]
        ] = {}
        self._progress_tasks: dict[tuple[str, str], set[asyncio.Task[None]]] = {}
        self._progress_usage: dict[tuple[str, str], tuple[int, int]] = {}
        self._capability_profiles: dict[str, dict[str, Any]] = {}
        self._reported_activity: dict[str, int] = {}
        self._reserved_slots: dict[tuple[str, WebSocket], int] = {}
        self._frame_sessions: dict[WebSocket, tuple[str, int]] = {}
        self._coding_waiters: dict[tuple[str, str], asyncio.Future[Any]] = {}
        self._coding_waiter_sockets: dict[tuple[str, str], WebSocket] = {}
        self._coding_expectations: dict[tuple[str, str], tuple[str, str]] = {}
        self._workspace_waiters: dict[tuple[str, str], asyncio.Future[Any]] = {}
        self._workspace_waiter_sockets: dict[tuple[str, str], WebSocket] = {}
        self._workspace_expectations: dict[
            tuple[str, str], tuple[str, str, str, str]
        ] = {}

    async def connect(self, agent_id: str, ws: WebSocket) -> None:
        await ws.accept()
        replaced = self._conns.get(agent_id)
        self._conns[agent_id] = ws
        if replaced is not None and replaced is not ws:
            self._fail_socket_requests(
                agent_id, replaced, "agent connection replaced"
            )
            self._frame_sessions.pop(replaced, None)
        self._capability_profiles[agent_id] = dict(LEGACY_AGENT_PROFILE)
        self._reported_activity[agent_id] = 0

        _broadcast_presence(
            agent_id, "connected", profile=self._capability_profiles[agent_id]
        )

    def disconnect(self, agent_id: str, ws: WebSocket) -> None:
        if self._conns.get(agent_id) is ws:
            self._fail_socket_requests(agent_id, ws, "agent disconnected")
            self._conns.pop(agent_id, None)
            self._capability_profiles.pop(agent_id, None)
            self._reported_activity.pop(agent_id, None)
            self._frame_sessions.pop(ws, None)

            _broadcast_presence(agent_id, "disconnected")

    def reset(self) -> None:
        """Drop live connections and cancel unresolved waits on app shutdown."""
        self._conns.clear()
        self._capability_profiles.clear()
        self._reported_activity.clear()
        self._reserved_slots.clear()
        self._frame_sessions.clear()
        for future in self._coding_waiters.values():
            if not future.done():
                future.cancel()
        self._coding_waiters.clear()
        self._coding_waiter_sockets.clear()
        self._coding_expectations.clear()
        for future in self._workspace_waiters.values():
            if not future.done():
                future.cancel()
        self._workspace_waiters.clear()
        self._workspace_waiter_sockets.clear()
        self._workspace_expectations.clear()
        for future in self._waiters.values():
            if not future.done():
                future.cancel()
        self._waiters.clear()
        self._waiter_sockets.clear()
        self._progress_handlers.clear()
        for tasks in self._progress_tasks.values():
            for task in tasks:
                task.cancel()
        self._progress_tasks.clear()
        self._progress_usage.clear()

    async def close(self, agent_id: str, code: int = 4004) -> None:
        ws = self._conns.pop(agent_id, None)
        self._capability_profiles.pop(agent_id, None)
        self._reported_activity.pop(agent_id, None)
        for key in [key for key in self._reserved_slots if key[0] == agent_id]:
            self._reserved_slots.pop(key, None)
        if ws is not None:
            self._fail_socket_requests(agent_id, ws, "agent connection closed")
            self._frame_sessions.pop(ws, None)
            await ws.close(code=code)

    def is_connected(self, agent_id: str) -> bool:
        return agent_id in self._conns

    def is_active_socket(self, agent_id: str, ws: WebSocket) -> bool:
        return self._conns.get(agent_id) is ws

    def _fail_socket_requests(
        self, agent_id: str, ws: WebSocket, reason: str
    ) -> None:
        for waiter_key, owner in list(self._waiter_sockets.items()):
            if waiter_key[0] != agent_id or owner is not ws:
                continue
            self._waiter_sockets.pop(waiter_key, None)
            future = self._waiters.pop(waiter_key, None)
            if future is not None and not future.done():
                future.set_exception(ConnectionError(reason))
            self._progress_handlers.pop(waiter_key, None)
            for task in self._progress_tasks.pop(waiter_key, set()):
                if not task.done():
                    task.cancel()
            self._progress_usage.pop(waiter_key, None)
        for waiter_key, owner in list(self._coding_waiter_sockets.items()):
            if waiter_key[0] != agent_id or owner is not ws:
                continue
            self._coding_waiter_sockets.pop(waiter_key, None)
            self._coding_expectations.pop(waiter_key, None)
            future = self._coding_waiters.pop(waiter_key, None)
            if future is not None and not future.done():
                future.set_exception(ConnectionError(reason))
        for waiter_key, owner in list(self._workspace_waiter_sockets.items()):
            if waiter_key[0] != agent_id or owner is not ws:
                continue
            self._workspace_waiter_sockets.pop(waiter_key, None)
            self._workspace_expectations.pop(waiter_key, None)
            future = self._workspace_waiters.pop(waiter_key, None)
            if future is not None and not future.done():
                future.set_exception(ConnectionError(reason))
        self._reserved_slots.pop((agent_id, ws), None)

    def validate_frame_sequence(
        self, agent_id: str, ws: WebSocket, frame: dict[str, Any]
    ) -> bool:
        if not self.is_active_socket(agent_id, ws):
            return False
        profile = self._capability_profiles.get(agent_id)
        if profile is None or profile["legacy"]:
            return True
        state = self._frame_sessions.get(ws)
        session_id = frame.get("session_id")
        seq = frame.get("seq")
        if (
            state is None
            or session_id != state[0]
            or not isinstance(seq, int)
            or isinstance(seq, bool)
            or seq <= state[1]
        ):
            return False
        self._frame_sessions[ws] = (state[0], seq)
        return True

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

    def capability_profile(self, agent_id: str) -> dict[str, Any] | None:
        profile = self._capability_profiles.get(agent_id)
        return dict(profile) if profile is not None else None

    def supports(self, agent_id: str, capability: str) -> bool:
        profile = self._capability_profiles.get(agent_id)
        return bool(profile and capability in profile["capabilities"])

    def is_available(self, agent_id: str) -> bool:
        profile = self._capability_profiles.get(agent_id)
        return bool(profile and profile["active_runs"] < profile["max_concurrency"])

    def _active_reserved_slots(self, agent_id: str) -> int:
        ws = self._conns.get(agent_id)
        return self._reserved_slots.get((agent_id, ws), 0) if ws is not None else 0

    def _effective_active_runs(self, agent_id: str) -> int:
        return self._reported_activity.get(agent_id, 0) + self._active_reserved_slots(agent_id)

    def _reserve_slot(self, agent_id: str) -> WebSocket:
        profile = self._capability_profiles.get(agent_id)
        if profile is None or profile["active_runs"] >= profile["max_concurrency"]:
            raise RuntimeError(f"agent {agent_id} is busy")
        ws = self._conns[agent_id]
        key = (agent_id, ws)
        self._reserved_slots[key] = self._reserved_slots.get(key, 0) + 1
        profile = {
            **profile,
            "active_runs": self._effective_active_runs(agent_id),
        }
        self._capability_profiles[agent_id] = profile
        _broadcast_presence(agent_id, "connected", profile=profile)
        return ws

    def _release_slot(self, agent_id: str, ws: WebSocket) -> None:
        key = (agent_id, ws)
        reserved = max(0, self._reserved_slots.get(key, 0) - 1)
        if reserved:
            self._reserved_slots[key] = reserved
        else:
            self._reserved_slots.pop(key, None)
        if self._conns.get(agent_id) is not ws:
            return
        profile = self._capability_profiles.get(agent_id)
        if profile is None:
            return
        profile = {
            **profile,
            "active_runs": self._effective_active_runs(agent_id),
        }
        self._capability_profiles[agent_id] = profile
        _broadcast_presence(agent_id, "connected", profile=profile)

    def negotiate_capabilities(
        self, agent_id: str, ws: WebSocket, payload: dict[str, Any]
    ) -> dict[str, Any]:
        if self._conns.get(agent_id) is not ws:
            raise ValueError("socket is not the active connection")
        if ws in self._frame_sessions:
            raise ValueError("capabilities already negotiated")
        protocol_version = payload.get("protocol_version")
        if protocol_version != AGENT_PROTOCOL_VERSION:
            raise ValueError("unsupported protocol version")
        agent_version = payload.get("agent_version")
        if not isinstance(agent_version, str) or not agent_version.strip() or len(agent_version) > 128:
            raise ValueError("invalid agent version")
        capabilities = payload.get("capabilities")
        if not isinstance(capabilities, list) or not all(
            isinstance(item, str) for item in capabilities
        ):
            raise ValueError("invalid capabilities")
        normalized = sorted(set(capabilities))
        unsupported = set(normalized) - AGENT_CAPABILITIES
        if unsupported:
            raise ValueError(f"unsupported capability: {sorted(unsupported)[0]}")
        max_concurrency = payload.get("max_concurrency")
        if (
            not isinstance(max_concurrency, int)
            or isinstance(max_concurrency, bool)
            or not 1 <= max_concurrency <= 64
        ):
            raise ValueError("invalid max concurrency")
        active_runs = max(
            self._reported_activity.get(agent_id, 0)
            + self._active_reserved_slots(agent_id),
            0,
        )
        if active_runs > max_concurrency:
            raise ValueError("max concurrency is below active runs")
        profile = {
            "protocol_version": protocol_version,
            "agent_version": agent_version.strip(),
            "capabilities": normalized,
            "max_concurrency": max_concurrency,
            "active_runs": active_runs,
            "legacy": False,
        }
        self._capability_profiles[agent_id] = profile
        session_id = secrets.token_urlsafe(24)
        self._frame_sessions[ws] = (session_id, 0)
        _broadcast_presence(agent_id, "connected", profile=profile)
        return {**profile, "session_id": session_id}

    def update_activity(
        self, agent_id: str, ws: WebSocket, active_runs: Any
    ) -> dict[str, Any]:
        if self._conns.get(agent_id) is not ws:
            raise ValueError("socket is not the active connection")
        profile = self._capability_profiles[agent_id]
        if (
            not isinstance(active_runs, int)
            or isinstance(active_runs, bool)
            or not 0 <= active_runs <= profile["max_concurrency"]
        ):
            raise ValueError("invalid active runs")
        if active_runs + self._active_reserved_slots(agent_id) > profile["max_concurrency"]:
            raise ValueError("active runs exceeds available capacity")
        self._reported_activity[agent_id] = active_runs
        profile = {
            **profile,
            "active_runs": active_runs + self._active_reserved_slots(agent_id),
        }
        self._capability_profiles[agent_id] = profile
        _broadcast_presence(agent_id, "connected", profile=profile)
        return dict(profile)

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

    async def send_and_wait(
        self,
        agent_id: str,
        payload: dict,
        timeout: float = 20.0,
        on_progress: Callable[[str, str], Awaitable[None]] | None = None,
        on_progress_complete: Callable[[str], Awaitable[None]] | None = None,
    ) -> Any:
        """Send a frame to a connected agent and await its correlated reply."""
        if agent_id not in self._conns:
            raise KeyError(f"agent {agent_id} not connected")
        reserved_socket = self._reserve_slot(agent_id)
        mid = payload.get("message_id") or self.new_message_id()
        payload = {**payload, "message_id": mid}
        loop = asyncio.get_event_loop()
        fut: asyncio.Future[Any] = loop.create_future()
        waiter_key = (agent_id, mid)
        self._waiters[waiter_key] = fut
        self._waiter_sockets[waiter_key] = reserved_socket
        if on_progress is not None:
            self._progress_handlers[waiter_key] = on_progress
            self._progress_tasks[waiter_key] = set()
            self._progress_usage[waiter_key] = (0, 0)
        try:
            await self.send(agent_id, payload)
            result = await asyncio.wait_for(fut, timeout=timeout)
            # The final reply has arrived, so the reply timeout is satisfied.
            # Give already-queued progress broadcasts a bounded chance to flush
            # before the persisted final message replaces the temporary output.
            tasks = list(self._progress_tasks.get(waiter_key, set()))
            if tasks:
                _, pending = await asyncio.wait(tasks, timeout=1.0)
                for task in pending:
                    task.cancel()
            return result
        finally:
            self._waiters.pop(waiter_key, None)
            self._waiter_sockets.pop(waiter_key, None)
            self._progress_handlers.pop(waiter_key, None)
            for task in self._progress_tasks.pop(waiter_key, set()):
                if not task.done():
                    task.cancel()
            self._progress_usage.pop(waiter_key, None)
            # Capacity is a server invariant: release it before any awaited,
            # best-effort UI cleanup so cancellation cannot strand a busy slot.
            self._release_slot(agent_id, reserved_socket)
            if on_progress_complete is not None:
                try:
                    await asyncio.wait_for(on_progress_complete(mid), timeout=1.0)
                except Exception:
                    # Completion is best-effort UI cleanup and must never mask
                    # the final reply or the original timeout/disconnect error.
                    pass

    async def deliver_progress(self, agent_id: str, message_id: str, text: str) -> bool:
        """Deliver progress only to the active request for this agent and message."""
        handler = self._progress_handlers.get((agent_id, message_id))
        if handler is None:
            return False
        waiter_key = (agent_id, message_id)
        frame_count, byte_count = self._progress_usage.get(waiter_key, (0, 0))
        text_bytes = len(text.encode("utf-8"))
        if frame_count >= 256 or byte_count + text_bytes > 1_048_576:
            return False
        self._progress_usage[waiter_key] = (frame_count + 1, byte_count + text_bytes)
        task = asyncio.ensure_future(handler(message_id, text))
        tasks = self._progress_tasks.setdefault(waiter_key, set())
        tasks.add(task)
        task.add_done_callback(tasks.discard)
        # Let the handler start so progress ordering is preserved without
        # awaiting slow channel clients to finish receiving the broadcast.
        await asyncio.sleep(0)
        return True

    def deliver_reply(self, agent_id: str, message_id: str, value: Any) -> bool:
        """Called by the agent WS loop when an agent sends a correlated reply."""
        return self._resolve(agent_id, message_id, value)

    async def send_coding_cancel(
        self,
        agent_id: str,
        *,
        run_id: str,
        request_id: str | None = None,
    ) -> Any:
        """Ask the agent to stop a running coding run (idempotent at the agent)."""
        if not self.supports(agent_id, "cancellation"):
            raise RuntimeError("unsupported capability: cancellation")
        if _SAFE_CODING_ID.fullmatch(run_id) is None:
            raise ValueError("run id contains unsafe characters")
        await self.send(
            agent_id,
            {
                "type": "coding_run_cancel",
                "request_id": request_id,
                "run_id": run_id,
            },
        )
        return {"type": "coding_run_ack"}

    async def send_coding_run(
        self,
        agent_id: str,
        *,
        repository_id: str,
        run_id: str,
        instruction: str,
        timeout: float,
        request_id: str | None = None,
    ) -> Any:
        """Dispatch opaque coding inputs; filesystem paths stay agent-local."""
        if not self.supports(agent_id, "coding_workspace"):
            raise RuntimeError("unsupported capability: coding_workspace")
        if _SAFE_CODING_ID.fullmatch(repository_id) is None:
            raise ValueError("repository id contains unsafe characters")
        if _SAFE_CODING_ID.fullmatch(run_id) is None:
            raise ValueError("run id contains unsafe characters")
        if not isinstance(instruction, str) or not instruction.strip() or len(instruction) > 65_536:
            raise ValueError("invalid coding instruction")
        reserved_socket = self._reserve_slot(agent_id)
        if request_id is None:
            request_id = self.new_message_id()
        key = (agent_id, request_id)
        future: asyncio.Future[Any] = asyncio.get_running_loop().create_future()
        self._coding_waiters[key] = future
        self._coding_waiter_sockets[key] = reserved_socket
        self._coding_expectations[key] = (repository_id, run_id)
        try:
            await self.send(
                agent_id,
                {
                    "type": "coding_run",
                    "request_id": request_id,
                    "repository_id": repository_id,
                    "run_id": run_id,
                    "instruction": instruction,
                },
            )
            return await asyncio.wait_for(future, timeout=timeout)
        finally:
            self._coding_waiters.pop(key, None)
            self._coding_waiter_sockets.pop(key, None)
            self._coding_expectations.pop(key, None)
            self._release_slot(agent_id, reserved_socket)

    def deliver_coding_change_set(
        self, agent_id: str, request_id: str, value: Any
    ) -> bool:
        try:
            change_set = self.validate_coding_change_set(agent_id, request_id, value)
        except ValueError as exc:
            future = self._coding_waiters.get((agent_id, request_id))
            if future is not None and not future.done():
                future.set_exception(exc)
            return False
        if change_set is None:
            return False
        return self.complete_coding_change_set(agent_id, request_id, change_set)

    def validate_coding_change_set(
        self, agent_id: str, request_id: str, value: Any
    ):
        from ..dto.change_sets import ChangeSetDTO

        key = (agent_id, request_id)
        future = self._coding_waiters.get(key)
        if future is None or future.done():
            return None
        expected = self._coding_expectations.get(key)
        change_set = ChangeSetDTO.model_validate(value)
        if expected != (change_set.repository_id, change_set.run_id):
            raise ValueError("coding change set does not match its request")
        return change_set

    def complete_coding_change_set(
        self, agent_id: str, request_id: str, change_set: Any
    ) -> bool:
        future = self._coding_waiters.get((agent_id, request_id))
        if future is None or future.done():
            return False
        future.set_result(change_set)
        return True

    def deliver_coding_failure(
        self, agent_id: str, request_id: str, error: Any
    ) -> bool:
        """Fail only the active correlated coding request for this identity."""
        future = self._coding_waiters.get((agent_id, request_id))
        if future is None or future.done():
            return False
        if not isinstance(error, str) or not error or len(error) > 4096:
            future.set_exception(ValueError("invalid coding failure payload"))
            return False
        future.set_exception(RuntimeError(error))
        return True

    async def send_workspace_action(
        self,
        agent_id: str,
        *,
        repository_id: str,
        run_id: str,
        branch: str,
        action: str,
        timeout: float,
    ) -> Any:
        if not self.supports(agent_id, "coding_workspace"):
            raise RuntimeError("unsupported capability: coding_workspace")
        if _SAFE_CODING_ID.fullmatch(repository_id) is None:
            raise ValueError("repository id contains unsafe characters")
        if _SAFE_CODING_ID.fullmatch(run_id) is None:
            raise ValueError("run id contains unsafe characters")
        if (
            not isinstance(branch, str)
            or len(branch) > 255
            or not branch.startswith("crewspace/")
            or any(part in {"", ".", ".."} for part in branch.split("/"))
        ):
            raise ValueError("invalid coding workspace branch")
        if action not in {"cleanup", "discard", "retain"}:
            raise ValueError("invalid coding workspace action")
        reserved_socket = self._reserve_slot(agent_id)
        request_id = self.new_message_id()
        key = (agent_id, request_id)
        future: asyncio.Future[Any] = asyncio.get_running_loop().create_future()
        self._workspace_waiters[key] = future
        self._workspace_waiter_sockets[key] = reserved_socket
        self._workspace_expectations[key] = (
            repository_id,
            run_id,
            branch,
            action,
        )
        try:
            if self._conns.get(agent_id) is not reserved_socket:
                raise ConnectionError("agent connection replaced")
            await reserved_socket.send_json(
                {
                    "type": "coding_workspace_action",
                    "request_id": request_id,
                    "repository_id": repository_id,
                    "run_id": run_id,
                    "branch": branch,
                    "action": action,
                },
            )
            return await asyncio.wait_for(future, timeout=timeout)
        finally:
            self._workspace_waiters.pop(key, None)
            self._workspace_waiter_sockets.pop(key, None)
            self._workspace_expectations.pop(key, None)
            self._release_slot(agent_id, reserved_socket)

    def deliver_workspace_action_result(
        self, agent_id: str, request_id: str, value: Any, socket: WebSocket
    ) -> bool:
        key = (agent_id, request_id)
        future = self._workspace_waiters.get(key)
        expected = self._workspace_expectations.get(key)
        if (
            future is None
            or future.done()
            or expected is None
            or self._workspace_waiter_sockets.get(key) is not socket
        ):
            return False
        if not isinstance(value, dict) or set(value) != {
            "repository_id", "run_id", "branch", "action", "status"
        }:
            future.set_exception(ValueError("invalid workspace action result"))
            return False
        received = (
            value["repository_id"],
            value["run_id"],
            value["branch"],
            value["action"],
        )
        allowed_statuses = {
            "cleanup": {"removed", "already_removed"},
            "discard": {"removed", "already_removed"},
            "retain": {"retained", "already_retained"},
        }
        if received != expected or value["status"] not in allowed_statuses[expected[3]]:
            future.set_exception(ValueError("workspace action result does not match request"))
            return False
        future.set_result(
            dict(
                zip(
                    ("repository_id", "run_id", "branch", "action"),
                    expected,
                    strict=True,
                )
            )
            | {"status": value["status"]}
        )
        return True

    def deliver_workspace_action_failure(
        self, agent_id: str, request_id: str, error: Any, socket: WebSocket
    ) -> bool:
        key = (agent_id, request_id)
        future = self._workspace_waiters.get(key)
        if (
            future is None
            or future.done()
            or self._workspace_waiter_sockets.get(key) is not socket
        ):
            return False
        if not isinstance(error, str) or not error or len(error) > 4096:
            future.set_exception(ValueError("invalid workspace action failure payload"))
            return False
        future.set_exception(RuntimeError(error))
        return True


def _broadcast_presence(
    agent_id: str, status: str, *, profile: dict[str, Any] | None = None
) -> None:
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
    payload: dict[str, Any] = {
        "type": "agent_presence",
        "agent_id": agent_id,
        "status": status,
    }
    if profile is not None:
        payload["profile"] = dict(profile)
    loop.create_task(manager.broadcast(PRESENCE_ROOM, payload))


manager = ConnectionManager()
agent_manager = AgentConnectionManager()
thread_manager = ConnectionManager()  # per-thread side-panel WebSockets

#: Broadcast channel that carries global agent-presence events to every open
#: UI client (the sidebar status dots subscribe here so they update live
#: without a page reload).
PRESENCE_ROOM = "__presence__"
