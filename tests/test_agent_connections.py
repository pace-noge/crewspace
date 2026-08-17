"""Remote-agent connection integrity regressions."""
from __future__ import annotations

import asyncio
from typing import cast

import pytest
from fastapi import WebSocket

from crewspace.api.connection import AgentConnectionManager


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
