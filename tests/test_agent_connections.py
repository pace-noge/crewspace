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
async def test_progress_is_bounded_per_request():
    manager = AgentConnectionManager()
    socket = FakeWebSocket()
    await manager.connect("agent_a", cast(WebSocket, socket))
    forwarded: list[str] = []

    async def on_progress(message_id: str, text: str) -> None:
        forwarded.append(text)

    pending = asyncio.create_task(
        manager.send_and_wait(
            "agent_a", {"type": "chat"}, timeout=0.1, on_progress=on_progress
        )
    )
    await asyncio.sleep(0)
    message_id = socket.sent[0]["message_id"]

    accepted = [
        await manager.deliver_progress("agent_a", message_id, "x")
        for _ in range(257)
    ]
    manager.deliver_reply("agent_a", message_id, "done")

    assert await pending == "done"
    assert accepted.count(True) <= 256
    assert accepted[-1] is False
    assert len(forwarded) <= 256


@pytest.mark.asyncio
async def test_progress_completion_runs_on_timeout_without_masking_result():
    manager = AgentConnectionManager()
    socket = FakeWebSocket()
    await manager.connect("agent_a", cast(WebSocket, socket))
    completed: list[str] = []

    async def on_progress(message_id: str, text: str) -> None:
        pass

    async def on_complete(message_id: str) -> None:
        completed.append(message_id)

    pending = asyncio.create_task(
        manager.send_and_wait(
            "agent_a",
            {"type": "chat"},
            timeout=0.01,
            on_progress=on_progress,
            on_progress_complete=on_complete,
        )
    )
    await asyncio.sleep(0)
    message_id = socket.sent[0]["message_id"]

    with pytest.raises(TimeoutError):
        await pending
    assert completed == [message_id]


@pytest.mark.asyncio
async def test_progress_completion_failure_does_not_mask_final_reply():
    manager = AgentConnectionManager()
    socket = FakeWebSocket()
    await manager.connect("agent_a", cast(WebSocket, socket))

    async def on_progress(message_id: str, text: str) -> None:
        pass

    async def on_complete(message_id: str) -> None:
        raise RuntimeError("chat client stalled")

    pending = asyncio.create_task(
        manager.send_and_wait(
            "agent_a",
            {"type": "chat"},
            timeout=0.05,
            on_progress=on_progress,
            on_progress_complete=on_complete,
        )
    )
    await asyncio.sleep(0)
    message_id = socket.sent[0]["message_id"]
    manager.deliver_reply("agent_a", message_id, "done")

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


@pytest.mark.asyncio
async def test_connected_agent_starts_with_explicit_legacy_capabilities():
    manager = AgentConnectionManager()
    socket = FakeWebSocket()

    await manager.connect("agent_a", cast(WebSocket, socket))

    profile = manager.capability_profile("agent_a")
    assert profile == {
        "protocol_version": 0,
        "agent_version": "legacy",
        "capabilities": ["progress", "tools"],
        "max_concurrency": 1,
        "active_runs": 0,
        "legacy": True,
    }
    assert manager.supports("agent_a", "progress") is True
    assert manager.supports("agent_a", "cancellation") is False


@pytest.mark.asyncio
async def test_agent_negotiates_versioned_capabilities_for_active_socket():
    manager = AgentConnectionManager()
    socket = FakeWebSocket()
    await manager.connect("agent_a", cast(WebSocket, socket))

    profile = manager.negotiate_capabilities(
        "agent_a",
        cast(WebSocket, socket),
        {
            "protocol_version": 1,
            "agent_version": "claude-code/1.0",
            "capabilities": ["progress", "cancellation", "artifacts"],
            "max_concurrency": 3,
        },
    )

    assert profile["legacy"] is False
    assert profile["max_concurrency"] == 3
    assert manager.supports("agent_a", "cancellation") is True
    assert manager.supports("agent_a", "tools") is False


@pytest.mark.asyncio
async def test_invalid_capability_profile_is_rejected_without_replacing_legacy_profile():
    manager = AgentConnectionManager()
    socket = FakeWebSocket()
    await manager.connect("agent_a", cast(WebSocket, socket))

    with pytest.raises(ValueError, match="unsupported capability"):
        manager.negotiate_capabilities(
            "agent_a",
            cast(WebSocket, socket),
            {
                "protocol_version": 1,
                "agent_version": "agent/1",
                "capabilities": ["root_shell"],
                "max_concurrency": 1,
            },
        )

    assert manager.capability_profile("agent_a")["legacy"] is True


@pytest.mark.asyncio
async def test_replaced_socket_cannot_overwrite_new_connection_capabilities():
    manager = AgentConnectionManager()
    old_socket = FakeWebSocket()
    new_socket = FakeWebSocket()
    await manager.connect("agent_a", cast(WebSocket, old_socket))
    await manager.connect("agent_a", cast(WebSocket, new_socket))

    with pytest.raises(ValueError, match="not the active connection"):
        manager.negotiate_capabilities(
            "agent_a",
            cast(WebSocket, old_socket),
            {
                "protocol_version": 1,
                "agent_version": "stale/1",
                "capabilities": ["progress"],
                "max_concurrency": 1,
            },
        )

    assert manager.capability_profile("agent_a")["legacy"] is True


@pytest.mark.asyncio
async def test_busy_slots_are_bounded_and_owned_by_active_socket():
    manager = AgentConnectionManager()
    old_socket = FakeWebSocket()
    new_socket = FakeWebSocket()
    await manager.connect("agent_a", cast(WebSocket, old_socket))
    manager.negotiate_capabilities(
        "agent_a",
        cast(WebSocket, old_socket),
        {
            "protocol_version": 1,
            "agent_version": "agent/1",
            "capabilities": ["progress"],
            "max_concurrency": 2,
        },
    )

    profile = manager.update_activity("agent_a", cast(WebSocket, old_socket), 2)
    assert profile["active_runs"] == 2
    assert manager.is_available("agent_a") is False
    with pytest.raises(ValueError, match="invalid active runs"):
        manager.update_activity("agent_a", cast(WebSocket, old_socket), 3)

    await manager.connect("agent_a", cast(WebSocket, new_socket))
    with pytest.raises(ValueError, match="not the active connection"):
        manager.update_activity("agent_a", cast(WebSocket, old_socket), 0)
    assert manager.capability_profile("agent_a")["active_runs"] == 0
    assert manager.is_available("agent_a") is True


@pytest.mark.asyncio
async def test_send_and_wait_reserves_slot_before_concurrent_dispatch():
    manager = AgentConnectionManager()
    socket = FakeWebSocket()
    await manager.connect("agent_a", cast(WebSocket, socket))
    manager.negotiate_capabilities(
        "agent_a",
        cast(WebSocket, socket),
        {
            "protocol_version": 1,
            "agent_version": "single-slot/1",
            "capabilities": [],
            "max_concurrency": 1,
        },
    )

    first = asyncio.create_task(
        manager.send_and_wait("agent_a", {"type": "chat"}, timeout=0.1)
    )
    await asyncio.sleep(0)
    assert manager.capability_profile("agent_a")["active_runs"] == 1

    with pytest.raises(RuntimeError, match="agent agent_a is busy"):
        await manager.send_and_wait("agent_a", {"type": "chat"}, timeout=0.01)

    manager.deliver_reply("agent_a", socket.sent[0]["message_id"], "done")
    assert await first == "done"
    assert manager.capability_profile("agent_a")["active_runs"] == 0


@pytest.mark.asyncio
async def test_agent_frames_cannot_clear_a_server_reserved_slot():
    manager = AgentConnectionManager()
    socket = FakeWebSocket()
    await manager.connect("agent_a", cast(WebSocket, socket))
    negotiated = {
        "protocol_version": 1,
        "agent_version": "single-slot/1",
        "capabilities": [],
        "max_concurrency": 1,
    }
    manager.negotiate_capabilities(
        "agent_a", cast(WebSocket, socket), negotiated
    )
    pending = asyncio.create_task(
        manager.send_and_wait("agent_a", {"type": "chat"}, timeout=0.1)
    )
    await asyncio.sleep(0)

    activity = manager.update_activity("agent_a", cast(WebSocket, socket), 0)
    with pytest.raises(ValueError, match="capabilities already negotiated"):
        manager.negotiate_capabilities(
            "agent_a", cast(WebSocket, socket), negotiated
        )

    assert activity["active_runs"] == 1
    assert manager.capability_profile("agent_a")["active_runs"] == 1
    assert manager.is_available("agent_a") is False
    manager.deliver_reply("agent_a", socket.sent[0]["message_id"], "done")
    assert await pending == "done"


@pytest.mark.asyncio
async def test_cancelled_wait_releases_reserved_slot_before_cleanup():
    manager = AgentConnectionManager()
    socket = FakeWebSocket()
    await manager.connect("agent_a", cast(WebSocket, socket))
    cleanup_started = asyncio.Event()

    async def on_complete(message_id: str) -> None:
        cleanup_started.set()
        await asyncio.sleep(10)

    pending = asyncio.create_task(
        manager.send_and_wait(
            "agent_a",
            {"type": "chat"},
            timeout=30,
            on_progress_complete=on_complete,
        )
    )
    await asyncio.sleep(0)
    pending.cancel()
    with pytest.raises(asyncio.CancelledError):
        await pending

    assert manager.capability_profile("agent_a")["active_runs"] == 0
    assert manager.is_available("agent_a") is True


@pytest.mark.asyncio
async def test_old_request_cleanup_cannot_release_replacement_socket_slot():
    manager = AgentConnectionManager()
    old_socket = FakeWebSocket()
    new_socket = FakeWebSocket()
    await manager.connect("agent_a", cast(WebSocket, old_socket))
    old = asyncio.create_task(
        manager.send_and_wait("agent_a", {"type": "chat"}, timeout=0.03)
    )
    await asyncio.sleep(0)

    await manager.connect("agent_a", cast(WebSocket, new_socket))
    new = asyncio.create_task(
        manager.send_and_wait("agent_a", {"type": "chat"}, timeout=0.1)
    )
    await asyncio.sleep(0)
    with pytest.raises(ConnectionError, match="agent connection replaced"):
        await old

    assert manager.capability_profile("agent_a")["active_runs"] == 1
    assert manager.is_available("agent_a") is False
    manager.deliver_reply("agent_a", new_socket.sent[0]["message_id"], "new")
    assert await new == "new"


@pytest.mark.asyncio
async def test_reconnect_immediately_fails_waits_owned_by_replaced_socket():
    manager = AgentConnectionManager()
    old_socket = FakeWebSocket()
    new_socket = FakeWebSocket()
    await manager.connect("agent_a", cast(WebSocket, old_socket))
    old = asyncio.create_task(
        manager.send_and_wait("agent_a", {"type": "chat"}, timeout=30)
    )
    await asyncio.sleep(0)

    await manager.connect("agent_a", cast(WebSocket, new_socket))

    with pytest.raises(ConnectionError, match="agent connection replaced"):
        await old
    assert not any(key[1] is old_socket for key in manager._reserved_slots)
    assert not manager._waiters


@pytest.mark.asyncio
async def test_disconnect_immediately_fails_waits_owned_by_socket():
    manager = AgentConnectionManager()
    socket = FakeWebSocket()
    await manager.connect("agent_a", cast(WebSocket, socket))
    pending = asyncio.create_task(
        manager.send_and_wait("agent_a", {"type": "chat"}, timeout=30)
    )
    await asyncio.sleep(0)

    manager.disconnect("agent_a", cast(WebSocket, socket))

    with pytest.raises(ConnectionError, match="agent disconnected"):
        await pending
    assert not manager._reserved_slots
    assert not manager._waiters


@pytest.mark.asyncio
async def test_reported_external_work_and_reserved_chat_use_separate_slots():
    manager = AgentConnectionManager()
    socket = FakeWebSocket()
    await manager.connect("agent_a", cast(WebSocket, socket))
    manager.negotiate_capabilities(
        "agent_a",
        cast(WebSocket, socket),
        {
            "protocol_version": 1,
            "agent_version": "two-slot/1",
            "capabilities": [],
            "max_concurrency": 2,
        },
    )
    manager.update_activity("agent_a", cast(WebSocket, socket), 1)

    pending = asyncio.create_task(
        manager.send_and_wait("agent_a", {"type": "chat"}, timeout=0.1)
    )
    await asyncio.sleep(0)

    assert manager.capability_profile("agent_a")["active_runs"] == 2
    assert manager.is_available("agent_a") is False
    manager.deliver_reply("agent_a", socket.sent[0]["message_id"], "done")
    assert await pending == "done"


@pytest.mark.asyncio
async def test_external_activity_cannot_overcommit_reserved_capacity():
    manager = AgentConnectionManager()
    socket = FakeWebSocket()
    await manager.connect("agent_a", cast(WebSocket, socket))
    manager.negotiate_capabilities(
        "agent_a",
        cast(WebSocket, socket),
        {
            "protocol_version": 1,
            "agent_version": "two-slot/1",
            "capabilities": [],
            "max_concurrency": 2,
        },
    )
    pending = asyncio.create_task(
        manager.send_and_wait("agent_a", {"type": "chat"}, timeout=0.1)
    )
    await asyncio.sleep(0)

    with pytest.raises(ValueError, match="exceeds available capacity"):
        manager.update_activity("agent_a", cast(WebSocket, socket), 2)

    assert manager.capability_profile("agent_a")["active_runs"] == 1
    manager.deliver_reply("agent_a", socket.sent[0]["message_id"], "done")
    assert await pending == "done"


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
            {
                "type": "agent_presence",
                "agent_id": "agent_x",
                "status": "connected",
                "profile": dict(conn.LEGACY_AGENT_PROFILE),
            },
        ),
        (
            conn.PRESENCE_ROOM,
            {"type": "agent_presence", "agent_id": "agent_x", "status": "disconnected"},
        ),
    ]
