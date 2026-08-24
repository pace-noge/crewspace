"""End-to-end agent cancellation against an in-process WebSocket server.

Drives the real example agent main() loop: the server negotiates the session,
sends a coding_run that runs a long `sleep`, then mid-run sends coding_run_cancel,
and we assert the agent terminates the subprocess and replies with a signed
coding_run_ack (status cancelled). This exercises the concurrent frame pump that
item 4 relies on (a cancel must be processable while a run executes).
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


def _make_key_env() -> tuple[str, str]:
    priv = Ed25519PrivateKey.generate()
    priv_b64u = base64.urlsafe_b64encode(priv.private_bytes_raw()).rstrip(b"=").decode()
    pub = priv.public_key()
    pub_b64u = base64.urlsafe_b64encode(pub.public_bytes_raw()).rstrip(b"=").decode()
    return priv_b64u, pub_b64u


async def _server(server_ready: asyncio.Future, got_ack: asyncio.Future, port_holder: dict) -> None:
    import websockets

    async def handler(ws):
        port_holder["port"] = ws.local_address[1]
        raw = await ws.recv()
        hello = json.loads(raw)
        assert hello["type"] == "hello"
        await ws.send(json.dumps({"type": "hello_ack", "session_id": "sess-1"}))
        server_ready.set_result(True)
        # Simulate the control plane dispatching a run to the agent.
        dispatch = {
            "type": "coding_run",
            "request_id": "req_e2e",
            "repository_id": "repo_e2e",
            "run_id": "run_e2e",
            "instruction": "sleep 30",
        }
        await ws.send(json.dumps(dispatch))
        # Let the agent start the subprocess, then cancel it mid-run.
        await asyncio.sleep(1.0)
        await ws.send(json.dumps({
            "type": "coding_run_cancel",
            "request_id": "req_e2e",
            "run_id": "run_e2e",
        }))
        # The agent should ack the cancellation (and may later send a change set).
        while not got_ack.done():
            msg = await asyncio.wait_for(ws.recv(), timeout=20.0)
            ack = json.loads(msg)
            if ack.get("type") == "coding_run_ack" and ack.get("status") == "cancelled":
                got_ack.set_result(ack)
                break

    async with websockets.serve(handler, "127.0.0.1", 0) as srv:
        port_holder["port"] = srv.sockets[0].getsockname()[1]
        # Signal readiness, then run until cancelled.
        await asyncio.Event().wait()  # run until cancelled


@pytest.mark.asyncio
async def test_agent_cancel_mid_run_via_real_loop(monkeypatch):
    if os.name == "nt":
        pytest.skip("posix subprocess semantics required")
    priv_b64u, _ = _make_key_env()
    port_holder: dict = {}
    server_ready = asyncio.get_event_loop().create_future()
    got_ack = asyncio.get_event_loop().create_future()

    server_task = asyncio.create_task(_server(server_ready, got_ack, port_holder))
    agent_task = None
    # Let the server bind and publish its port before we read it.
    await asyncio.sleep(0.1)
    try:
        monkeypatch.setenv("AGENT_ID", "agent_cancel_e2e")
        monkeypatch.setenv("AGENT_PRIV", priv_b64u)
        monkeypatch.setenv("AGENT_WS_URL", f"ws://127.0.0.1:{port_holder['port']}")
        # A long sleep so the run is still executing when cancel arrives.
        monkeypatch.setenv("CLAUDE_BIN", "sleep")
        monkeypatch.setenv("CLAUDE_ARGS", "30")

        agent_task = asyncio.create_task(agent_mod.main())
        await asyncio.wait_for(server_ready, timeout=10.0)
        ack = await asyncio.wait_for(got_ack, timeout=25.0)
        assert ack["status"] == "cancelled"
        assert ack["run_id"]
        assert ack["sig"]
    finally:
        if agent_task is not None:
            agent_task.cancel()
        server_task.cancel()
        for t in (agent_task, server_task):
            if t is None:
                continue
            try:
                await t
            except (asyncio.CancelledError, Exception):
                pass
