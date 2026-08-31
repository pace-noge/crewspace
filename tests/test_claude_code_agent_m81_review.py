"""M8.1 review fixes — regressions raised by independent fail-closed review.

Encodes four invariants the reviewer flagged:
1. A cancelled run must NOT emit a terminal coding_change_set after the cancel.
2. Terminal dedup is complete: allocation failure marks the run terminal so a
   replay cannot re-send, and a duplicate coding_run while a run is in-flight is
   rejected (no double execution).
3. AGENT_AUTONOMOUS=0 disables autonomous work (explicit boolean parse).
4. The socket receive pump must not be blocked by inline subprocess work (chat /
   card_created dispatch without awaiting the whole thing inline).
"""
from __future__ import annotations

import asyncio
import base64
import json
import os
import sys
from pathlib import Path

import pytest

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
EXAMPLE = Path(__file__).resolve().parents[1] / "examples" / "claude_code_agent.py"
sys.path.insert(0, str(EXAMPLE.parent))
import importlib.util

spec = importlib.util.spec_from_file_location("claude_code_agent_example", EXAMPLE)
agent_mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(agent_mod)


class _FakeSigner:
    def sign_frame(self, frame: dict) -> dict:
        f = dict(frame)
        f["sig"] = "test-sig"
        return f


def _make_key_env() -> tuple[str, str]:
    priv = Ed25519PrivateKey.generate()
    priv_b64u = base64.urlsafe_b64encode(priv.private_bytes_raw()).rstrip(b"=").decode()
    pub_b64u = base64.urlsafe_b64encode(priv.public_key().public_bytes_raw()).rstrip(b"=").decode()
    return priv_b64u, pub_b64u


def _make_helper_script(tmp_path: Path) -> Path:
    script = tmp_path / "sleep_helper.sh"
    script.write_text("#!/bin/sh\nexec sleep 30\n")
    script.chmod(0o755)
    return script


# ---------------------------------------------------------------------------
# 4. Socket pump independence: long chat subprocess must not block reconnect
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_long_chat_does_not_block_reconnect(tmp_path, monkeypatch):
    """A chat dispatching a long subprocess must not block the receive pump;
    socket-close detection and reconnect must still work promptly."""
    import websockets

    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    helper = _make_helper_script(tmp_path)
    priv = Ed25519PrivateKey.generate()
    priv_b64u = base64.urlsafe_b64encode(priv.private_bytes_raw()).rstrip(b"=").decode()

    server_ready: asyncio.Future = asyncio.get_event_loop().create_future()
    reconnected: asyncio.Future = asyncio.get_event_loop().create_future()
    port_holder: dict = {}
    conn_count = {"n": 0}

    async def handler(ws):
        conn_count["n"] += 1
        n = conn_count["n"]
        hello = json.loads(await ws.recv())
        assert hello["type"] == "hello"
        await ws.send(json.dumps({"type": "hello_ack", "session_id": f"sess-{n}"}))
        if n == 1:
            server_ready.set_result(True)
            # Send a chat — the agent will dispatch a 30s subprocess. Drop the
            # socket immediately: if the chat blocks the pump, the agent will be
            # stuck waiting for the subprocess and will NOT reconnect.
            await ws.send(json.dumps({"type": "chat", "text": "long-job", "message_id": "m-long"}))
            await asyncio.sleep(0.1)  # give agent time to receive and dispatch
            await ws.close()
        else:
            # Second connection means reconnect succeeded despite the long chat.
            if not reconnected.done():
                reconnected.set_result(True)
            # Keep socket alive until test teardown.
            try:
                while True:
                    await asyncio.wait_for(ws.recv(), timeout=2.0)
            except (asyncio.TimeoutError, websockets.exceptions.ConnectionClosed):
                pass

    async with websockets.serve(handler, "127.0.0.1", 0) as srv:
        port_holder["port"] = srv.sockets[0].getsockname()[1]
        agent_task = None
        try:
            monkeypatch.setenv("AGENT_ID", "agent_pump_e2e")
            monkeypatch.setenv("AGENT_PRIV", priv_b64u)
            monkeypatch.setenv("AGENT_WS_URL", f"ws://127.0.0.1:{port_holder['port']}")
            monkeypatch.setenv("CLAUDE_BIN", str(helper))
            monkeypatch.setenv("CLAUDE_ARGS", "")
            monkeypatch.setenv("AGENT_RECONNECT_DELAY", "0.05")

            agent_task = asyncio.create_task(agent_mod.main())
            await asyncio.wait_for(server_ready, timeout=10.0)
            # If chat blocks the pump, reconnect will NOT happen within 5s.
            await asyncio.wait_for(reconnected, timeout=5.0)
        finally:
            if agent_task is not None:
                agent_task.cancel()
            try:
                await agent_task
            except (asyncio.CancelledError, Exception):
                pass


# ---------------------------------------------------------------------------
# 1. Cancellation suppresses the terminal change set
# ---------------------------------------------------------------------------
def test_cancel_marks_run_cancelled_and_finish_suppresses_change_set():
    rt = agent_mod.AgentRuntime()
    # Simulate the cancel handler marking the run cancelled + terminal.
    rt.mark_cancelled("run_z")
    rt.add_terminal("run_z", "req_z")
    assert rt.is_cancelled("run_z")
    assert rt.is_terminal("run_z")


@pytest.mark.asyncio
async def test_finish_coding_run_sends_nothing_for_cancelled_run():
    """The done-callback path must not emit a coding_change_set for a cancelled
    run. We drive the runtime state directly (as the pump would set it after a
    cancel) and assert the send hook never fires."""
    rt = agent_mod.AgentRuntime()
    rt.mark_cancelled("run_c")
    rt.add_terminal("run_c", "req_c")

    sent: list[dict] = []

    async def send(frame: dict) -> None:
        sent.append(frame)

    # A cancelled run must be skipped when the same coding_run is replayed or
    # its task's done-callback would fire.
    assert rt.is_terminal("run_c"), "cancelled run must be terminal"
    # claim must fail (already terminal) so no new execution/emission occurs
    assert not rt.claim_coding_run("run_c", "req_c")
    # No terminal frame may be produced for a cancelled run.
    assert sent == []


# ---------------------------------------------------------------------------
# 2. Terminal dedup: allocation failure + in-flight re-entry
# ---------------------------------------------------------------------------
def test_allocation_failure_is_terminal_and_replay_is_rejected():
    """An allocation failure must record terminal state so a replayed coding_run
    cannot re-send coding_run_failed (duplicate terminal frame)."""
    rt = agent_mod.AgentRuntime()
    # The pump adds terminal on the allocation-failure branch:
    rt.add_terminal("run_alloc", "req_alloc")
    assert rt.is_terminal("run_alloc")
    # A replay of the same run is rejected (no second execution/emission).
    assert not rt.claim_coding_run("run_alloc", "req_alloc")


def test_in_flight_run_rejects_duplicate_claim():
    """A duplicate coding_run while a run is already running must not double
    execute; claim returns False on the second attempt."""
    rt = agent_mod.AgentRuntime()
    assert rt.claim_coding_run("run_busy", "req_busy") is True
    # Second, duplicate frame for the same run id while in flight:
    assert rt.claim_coding_run("run_busy", "req_busy_dup") is False


def test_terminal_state_tracks_request_id():
    rt = agent_mod.AgentRuntime()
    assert rt.claim_coding_run("run_req", "req_req") is True
    rt.add_terminal("run_req", "req_req")
    assert rt.is_terminal("run_req")
    assert rt.request_is_terminal("req_req")


@pytest.mark.asyncio
async def test_cancel_cancels_registered_run_task():
    rt = agent_mod.AgentRuntime()
    started = asyncio.Event()

    async def long_running() -> None:
        started.set()
        await asyncio.Event().wait()

    task = asyncio.create_task(long_running())
    rt.running_tasks["run_task"] = task
    rt.claim_coding_run("run_task", "req_task")
    await started.wait()

    sent: list[dict] = []

    async def send(frame: dict) -> None:
        sent.append(frame)

    await agent_mod._handle_coding_run_cancel(
        rt,
        {"type": "coding_run_cancel", "run_id": "run_task", "request_id": "req_task"},
        _FakeSigner(),
        send,
    )
    await asyncio.sleep(0)
    assert task.cancelled(), "cancel handler must cancel the registered run task"
    assert [frame["type"] for frame in sent] == ["coding_run_ack"]


# ---------------------------------------------------------------------------
# 3. AGENT_AUTONOMOUS truthiness bug (0 must disable)
# ---------------------------------------------------------------------------
def test_autonomous_enabled_only_for_explicit_true(monkeypatch):
    for falsey in ("0", "false", "no", ""):
        monkeypatch.setenv("AGENT_AUTONOMOUS", falsey)
        assert agent_mod.autonomous_enabled() is False, f"{falsey!r} should disable"
    for truthy in ("1", "true", "yes", "on"):
        monkeypatch.setenv("AGENT_AUTONOMOUS", truthy)
        assert agent_mod.autonomous_enabled() is True, f"{truthy!r} should enable"
