"""Remote-agent connection integrity regressions."""
from __future__ import annotations

import asyncio
from typing import cast

import pytest
from fastapi import WebSocket

from crewspace.api.connection import AgentConnectionManager, ConnectionManager
from crewspace.dto.change_sets import ChangeSetDTO


class FakeWebSocket:
    def __init__(self) -> None:
        self.sent: list[dict] = []

    async def accept(self) -> None:
        pass

    async def send_json(self, payload: dict) -> None:
        self.sent.append(payload)

    async def close(self, code: int) -> None:
        pass


@pytest.mark.parametrize(
    "field,value",
    [
        (
            "files",
            [
                {
                    "path": f"file-{index}.py",
                    "status": "added",
                    "additions": 1,
                    "deletions": 0,
                }
                for index in range(1001)
            ],
        ),
        ("artifacts", [{"path": f"artifact-{index}", "size_bytes": 1} for index in range(65)]),
        ("commits", [{"sha": "g" * 40, "subject": "invalid sha"}]),
        ("commits", [{"sha": "a" * 40 + "TRAIL", "subject": "suffix"}]),
        ("artifacts", [{"path": "../private-key", "size_bytes": 1}]),
        ("artifacts", [{"path": r"C:\private-key", "size_bytes": 1}]),
        ("artifacts", [{"path": "a//b", "size_bytes": 1}]),
        ("artifacts", [{"path": "a/./b", "size_bytes": 1}]),
        ("artifacts", [{"path": ".", "size_bytes": 1}]),
    ],
)
def test_change_set_wire_metadata_is_bounded_and_relative(field, value):
    payload = {
        "repository_id": "crewspace",
        "run_id": "run_123",
        "branch": "crewspace/run_123-deadbeef",
        "base_commit": "a" * 40,
        "head_commit": "b" * 40,
        "commits": [],
        "files": [],
        "additions": 0,
        "deletions": 0,
        "verification": [],
        "artifacts": [],
        field: value,
    }

    with pytest.raises(ValueError):
        ChangeSetDTO.model_validate(payload)


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
async def test_coding_run_returns_correlated_remote_change_set():
    manager = AgentConnectionManager()
    socket = FakeWebSocket()
    await manager.connect("agent_a", cast(WebSocket, socket))
    manager.negotiate_capabilities(
        "agent_a",
        cast(WebSocket, socket),
        {
            "protocol_version": 1,
            "agent_version": "claude-code/2.0",
            "capabilities": ["coding_workspace"],
            "max_concurrency": 1,
        },
    )

    pending = asyncio.create_task(
        manager.send_coding_run(
            "agent_a",
            repository_id="crewspace",
            run_id="run_123",
            instruction="Implement the requested change",
            timeout=0.1,
        )
    )
    await asyncio.sleep(0)
    request = socket.sent[0]

    assert request == {
        "type": "coding_run",
        "request_id": request["request_id"],
        "repository_id": "crewspace",
        "run_id": "run_123",
        "instruction": "Implement the requested change",
    }
    change_set = {
        "repository_id": "crewspace",
        "run_id": "run_123",
        "branch": "crewspace/run_123-deadbeef",
        "base_commit": "a" * 40,
        "head_commit": "b" * 40,
        "commits": [],
        "files": [],
        "additions": 0,
        "deletions": 0,
        "verification": [],
        "artifacts": [],
    }
    assert manager.deliver_coding_change_set(
        "agent_a", request["request_id"], change_set
    ) is True
    result = await pending
    assert isinstance(result, ChangeSetDTO)
    assert result.model_dump(mode="json") == change_set


@pytest.mark.asyncio
async def test_coding_run_remote_failure_is_correlated_and_releases_capacity():
    manager = AgentConnectionManager()
    socket = FakeWebSocket()
    await manager.connect("agent_a", cast(WebSocket, socket))
    manager.negotiate_capabilities(
        "agent_a",
        cast(WebSocket, socket),
        {
            "protocol_version": 1,
            "agent_version": "claude-code/2.0",
            "capabilities": ["coding_workspace"],
            "max_concurrency": 1,
        },
    )
    pending = asyncio.create_task(
        manager.send_coding_run(
            "agent_a",
            repository_id="crewspace",
            run_id="run_123",
            instruction="change it",
            timeout=1.0,
        )
    )
    await asyncio.sleep(0)
    request_id = socket.sent[0]["request_id"]

    assert manager.deliver_coding_failure(
        "agent_a", request_id, "workspace allocation failed"
    ) is True
    with pytest.raises(RuntimeError, match="workspace allocation failed"):
        await pending
    assert manager.capability_profile("agent_a")["active_runs"] == 0
    assert manager.is_available("agent_a") is True

    assert manager.deliver_coding_failure(
        "agent_a", "different-request", "forged"
    ) is False


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "change_set",
    [
        {
            "repository_id": "other",
            "run_id": "run_123",
            "branch": "crewspace/run_123-deadbeef",
            "base_commit": "a" * 40,
            "head_commit": "b" * 40,
            "commits": [], "files": [], "additions": 0, "deletions": 0,
            "verification": [], "artifacts": [],
        },
        {
            "repository_id": "crewspace",
            "run_id": "run_123",
            "path": "/agent/private/worktree",
            "branch": "crewspace/run_123-deadbeef",
            "base_commit": "a" * 40,
            "head_commit": "b" * 40,
            "commits": [], "files": [], "additions": 0, "deletions": 0,
            "verification": [], "artifacts": [],
        },
    ],
)
async def test_coding_run_rejects_uncorrelated_or_path_bearing_results(change_set):
    manager = AgentConnectionManager()
    socket = FakeWebSocket()
    await manager.connect("agent_a", cast(WebSocket, socket))
    manager.negotiate_capabilities(
        "agent_a",
        cast(WebSocket, socket),
        {
            "protocol_version": 1,
            "agent_version": "claude-code/2.0",
            "capabilities": ["coding_workspace"],
            "max_concurrency": 1,
        },
    )
    pending = asyncio.create_task(
        manager.send_coding_run(
            "agent_a",
            repository_id="crewspace",
            run_id="run_123",
            instruction="change it",
            timeout=0.1,
        )
    )
    await asyncio.sleep(0)

    assert manager.deliver_coding_change_set(
        "agent_a", socket.sent[0]["request_id"], change_set
    ) is False
    with pytest.raises(ValueError):
        await pending


@pytest.mark.asyncio
async def test_coding_run_requires_negotiated_remote_workspace_capability():
    manager = AgentConnectionManager()
    socket = FakeWebSocket()
    await manager.connect("agent_a", cast(WebSocket, socket))

    with pytest.raises(RuntimeError, match="unsupported capability: coding_workspace"):
        await manager.send_coding_run(
            "agent_a",
            repository_id="crewspace",
            run_id="run_123",
            instruction="change it",
            timeout=0.01,
        )

    assert socket.sent == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "repository_id,run_id", [("../repo", "run_123"), ("repo", "../run")]
)
async def test_coding_run_rejects_unsafe_opaque_identifiers(repository_id, run_id):
    manager = AgentConnectionManager()
    socket = FakeWebSocket()
    await manager.connect("agent_a", cast(WebSocket, socket))
    manager.negotiate_capabilities(
        "agent_a",
        cast(WebSocket, socket),
        {
            "protocol_version": 1,
            "agent_version": "claude-code/2.0",
            "capabilities": ["coding_workspace"],
            "max_concurrency": 1,
        },
    )

    with pytest.raises(ValueError, match="unsafe"):
        await manager.send_coding_run(
            "agent_a",
            repository_id=repository_id,
            run_id=run_id,
            instruction="change it",
            timeout=0.01,
        )

    assert socket.sent == []


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
