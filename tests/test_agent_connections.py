"""Remote-agent connection integrity regressions."""
from __future__ import annotations

import asyncio
from typing import cast

import pytest
from fastapi import WebSocket

from crewspace.api.connection import AgentConnectionManager, ConnectionManager


class FakeWebSocket:
    def __init__(self) -> None:
        self.sent: list[dict] = []

    async def accept(self) -> None:
        pass

    async def send_json(self, payload: dict) -> None:
        self.sent.append(payload)

    async def close(self, code: int) -> None:
        pass


@pytest.mark.asyncio
async def test_reply_must_come_from_expected_agent():
    manager = AgentConnectionManager()
    socket = FakeWebSocket()
    await manager.connect("agent_a", cast(WebSocket, socket))

    pending = asyncio.create_task(
        manager.send_and_wait("agent_a", {"type": "chat"}, timeout=0.05)
    )
    await asyncio.sleep(0)
    message_id = socket.sent[0]["message_id"]

    assert manager.deliver_reply("agent_b", message_id, "spoofed") is False
    with pytest.raises(TimeoutError):
        await pending


@pytest.mark.asyncio
async def test_progress_is_delivered_before_correlated_reply():
    manager = AgentConnectionManager()
    socket = FakeWebSocket()
    await manager.connect("agent_a", cast(WebSocket, socket))
    events: list[tuple[str, str]] = []

    async def on_progress(message_id: str, text: str) -> None:
        assert message_id == socket.sent[0]["message_id"]
        events.append(("progress", text))

    pending = asyncio.create_task(
        manager.send_and_wait(
            "agent_a", {"type": "chat"}, timeout=0.05, on_progress=on_progress
        )
    )
    await asyncio.sleep(0)
    message_id = socket.sent[0]["message_id"]

    assert await manager.deliver_progress("agent_a", message_id, "first line") is True
    assert await manager.deliver_progress("agent_b", message_id, "spoofed") is False
    events.append(("reply", "final answer"))
    assert manager.deliver_reply("agent_a", message_id, "final answer") is True

    assert await pending == "final answer"
    assert events == [("progress", "first line"), ("reply", "final answer")]


@pytest.mark.asyncio
async def test_slow_progress_handler_does_not_consume_reply_timeout():
    manager = AgentConnectionManager()
    socket = FakeWebSocket()
    await manager.connect("agent_a", cast(WebSocket, socket))
    release_progress = asyncio.Event()

    async def on_progress(message_id: str, text: str) -> None:
        await release_progress.wait()

    pending = asyncio.create_task(
        manager.send_and_wait(
            "agent_a", {"type": "chat"}, timeout=0.05, on_progress=on_progress
        )
    )
    await asyncio.sleep(0)
    message_id = socket.sent[0]["message_id"]

    assert await manager.deliver_progress("agent_a", message_id, "blocked") is True
    assert manager.deliver_reply("agent_a", message_id, "done") is True
    release_progress.set()

    assert await pending == "done"


@pytest.mark.asyncio
async def test_old_socket_disconnect_does_not_remove_replacement():
    manager = AgentConnectionManager()
    old_socket = FakeWebSocket()
    new_socket = FakeWebSocket()
    await manager.connect("agent_a", cast(WebSocket, old_socket))
    await manager.connect("agent_a", cast(WebSocket, new_socket))

    manager.disconnect("agent_a", cast(WebSocket, old_socket))

    assert manager.is_connected("agent_a")
    await manager.send("agent_a", {"type": "probe"})
    assert new_socket.sent == [{"type": "probe"}]


def test_agent_request_ids_are_not_sequential():
    manager = AgentConnectionManager()

    first = manager.new_message_id()
    second = manager.new_message_id()

    assert first != "m1"
    assert second != "m2"
    assert first != second


def test_agent_status_distinguishes_builtin_and_remote_agents():
    manager = AgentConnectionManager()

    assert manager.status("agent_builtin", is_local=True) == "local"
    assert manager.status("agent_remote", is_local=False) == "disconnected"


class RecordingManager(ConnectionManager):
    def __init__(self) -> None:
        super().__init__()
        self.broadcasts: list[tuple[str, dict]] = []

    async def broadcast(self, channel_id: str, payload: dict) -> None:
        self.broadcasts.append((channel_id, payload))


@pytest.mark.asyncio
async def test_connect_and_disconnect_broadcast_presence(monkeypatch):
    import crewspace.api.connection as conn

    recorder = RecordingManager()
    monkeypatch.setattr(conn, "manager", recorder)
    manager = AgentConnectionManager()
    ws = FakeWebSocket()

    await manager.connect("agent_x", cast(WebSocket, ws))
    manager.disconnect("agent_x", cast(WebSocket, ws))
    # Presence is broadcast fire-and-forget on the running loop; let the
    # scheduled tasks run before inspecting the recorded frames.
    await asyncio.sleep(0)

    assert recorder.broadcasts == [
        (
            conn.PRESENCE_ROOM,
            {"type": "agent_presence", "agent_id": "agent_x", "status": "connected"},
        ),
        (
            conn.PRESENCE_ROOM,
            {"type": "agent_presence", "agent_id": "agent_x", "status": "disconnected"},
        ),
    ]
