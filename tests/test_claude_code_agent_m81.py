"""M8.1 — reference remote coding agent: reconnect/resume + signed agent_activity.

Covers the two behaviors the reference agent previously lacked: a robust
reconnect loop (a dropped socket must not kill the process; a fresh connect
claim + session is negotiated and the agent keeps serving), and signed
``agent_activity`` publishing for autonomous work it starts outside a
Crewspace-reserved slot.
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


async def _negotiate_and_run(
    server_ready: asyncio.Future, got_second_reply: asyncio.Future, port_holder: dict
) -> None:
    """Accept TWO sequential connections; each must get its own session + reply.

    Connection 1: negotiate session 'sess-1', send chat A, await reply A, then
    close (simulate a dropped socket). Connection 2: negotiate session 'sess-2',
    send chat B, await reply B.
    """
    import websockets

    conn_count = {"n": 0}

    async def handler(ws):
        conn_count["n"] += 1
        n = conn_count["n"]
        raw = await ws.recv()
        hello = json.loads(raw)
        assert hello["type"] == "hello"
        # The connect claim is rebuilt per reconnect; unverified here (server test
        # verifies the claim in the app path). Send a fresh session per connection.
        await ws.send(
            json.dumps(
                {"type": "hello_ack", "session_id": f"sess-{n}", "capabilities": []}
            )
        )
        if n == 1:
            server_ready.set_result(True)
            await ws.send(
                json.dumps(
                    {
                        "type": "chat",
                        "text": "reply-me-a",
                        "message_id": "m-a",
                    }
                )
            )
            # Await the reply, then drop the socket.
            while True:
                msg = json.loads(await asyncio.wait_for(ws.recv(), timeout=20.0))
                if msg.get("type") == "reply" and msg.get("message_id") == "m-a":
                    break
            await ws.close()
        else:
            await ws.send(
                json.dumps(
                    {
                        "type": "chat",
                        "text": "reply-me-b",
                        "message_id": "m-b",
                    }
                )
            )
            while not got_second_reply.done():
                msg = json.loads(await asyncio.wait_for(ws.recv(), timeout=20.0))
                if msg.get("type") == "reply" and msg.get("message_id") == "m-b":
                    got_second_reply.set_result(msg)
                    break

    async with websockets.serve(handler, "127.0.0.1", 0) as srv:
        port_holder["port"] = srv.sockets[0].getsockname()[1]
        await asyncio.Event().wait()  # run until cancelled


@pytest.mark.asyncio
async def test_agent_reconnects_after_socket_drop_and_resumes(monkeypatch):
    if os.name == "nt":
        pytest.skip("posix subprocess semantics required")
    priv_b64u, _ = _make_key_env()
    port_holder: dict = {}
    server_ready = asyncio.get_event_loop().create_future()
    got_second_reply = asyncio.get_event_loop().create_future()

    server_task = asyncio.create_task(
        _negotiate_and_run(server_ready, got_second_reply, port_holder)
    )
    agent_task = None
    await asyncio.sleep(0.1)
    try:
        monkeypatch.setenv("AGENT_ID", "agent_resume_e2e")
        monkeypatch.setenv("AGENT_PRIV", priv_b64u)
        monkeypatch.setenv("AGENT_WS_URL", f"ws://127.0.0.1:{port_holder['port']}")
        # A fast, harmless command so a chat reply is produced without a real CLI.
        monkeypatch.setenv("CLAUDE_BIN", "printf")
        monkeypatch.setenv("CLAUDE_ARGS", "%s")
        monkeypatch.setenv("AGENT_RECONNECT_DELAY", "0.05")

        agent_task = asyncio.create_task(agent_mod.main())
        await asyncio.wait_for(server_ready, timeout=10.0)
        # The second connection carries a NEW session (reconnect), so the agent
        # renegotiated rather than reusing the dropped one.
        second_reply = await asyncio.wait_for(got_second_reply, timeout=25.0)
        assert second_reply["message_id"] == "m-b"
        assert second_reply["sig"]
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


def test_negotiates_only_implemented_capabilities():
    """The hello frame must advertise ONLY capabilities the agent backs with
    handlers in the frame pump."""
    implemented = {
        ftype
        for ftype in (
            "chat",
            "coding_run",
            "coding_run_cancel",
            "coding_workspace_action",
            "card_created",
            "tool_result",
        )
    }
    # These map to negotiated capability strings in the hello payload.
    capability_of = {
        "chat": "progress",
        "coding_run": "coding_workspace",
        "coding_run_cancel": "cancellation",
        "coding_workspace_action": "coding_workspace",
    }
    # The agent must not negotiate a capability it has no handler for.
    declared = {
        "progress",
        "coding_workspace",
        "cancellation",
        "tools",
        "artifacts",
        "patches",
        "resume",
        "heartbeat",
    }
    # Find the negotiated list in the source hello and assert it's subset of
    # what the pump actually handles.
    source = EXAMPLE.read_text()
    import re

    m = re.search(r"\"capabilities\":\s*\[([^\]]*)\]", source)
    assert m, "hello must declare capabilities"
    negotiated = {c.strip().strip('"') for c in m.group(1).split(",")}
    # Every negotiated capability is one the code path handles.
    for cap in negotiated:
        assert cap in declared
    # coding_workspace and progress and cancellation are implemented.
    assert {"progress", "coding_workspace", "cancellation"} <= negotiated
    # We do NOT declare tools/artifacts/patches/resume/heartbeat (no handlers).
    assert not (negotiated & {"tools", "artifacts", "patches", "resume", "heartbeat"})


async def _collect_activity_frames(server_ready, got_activity, port_holder):
    """Server: negotiate, push a card_created (autonomous trigger), and expect a
    signed agent_activity from the agent reporting its external work."""
    import websockets

    async def handler(ws):
        raw = await ws.recv()
        hello = json.loads(raw)
        await ws.send(json.dumps({"type": "hello_ack", "session_id": "sess-a"}))
        server_ready.set_result(True)
        # Push an autonomous trigger: a card created elsewhere. With
        # AGENT_AUTONOMOUS=1 the agent reacts and publishes activity.
        await ws.send(
            json.dumps(
                {
                    "type": "card_created",
                    "card": {
                        "id": "card_auto",
                        "column_id": "col_todo",
                        "title": "an autonomous card",
                        "description": None,
                        "assignee_id": None,
                    },
                }
            )
        )
        activity_frames = []
        while not got_activity.done():
            msg = json.loads(await asyncio.wait_for(ws.recv(), timeout=20.0))
            if msg.get("type") == "agent_activity":
                activity_frames.append(msg)
                if len(activity_frames) == 2:
                    got_activity.set_result(activity_frames)
                    break

    async with websockets.serve(handler, "127.0.0.1", 0) as srv:
        port_holder["port"] = srv.sockets[0].getsockname()[1]
        await asyncio.Event().wait()


@pytest.mark.asyncio
async def test_agent_publishes_signed_agent_activity(monkeypatch):
    """With autonomous work enabled, reacting to a card_created publishes a
    signed agent_activity so the app reflects the external slot usage."""
    if os.name == "nt":
        pytest.skip("posix subprocess semantics required")
    priv_b64u, _ = _make_key_env()
    port_holder: dict = {}
    server_ready = asyncio.get_event_loop().create_future()
    got_activity = asyncio.get_event_loop().create_future()

    server_task = asyncio.create_task(
        _collect_activity_frames(server_ready, got_activity, port_holder)
    )
    agent_task = None
    await asyncio.sleep(0.1)
    try:
        monkeypatch.setenv("AGENT_ID", "agent_activity_e2e")
        monkeypatch.setenv("AGENT_PRIV", priv_b64u)
        monkeypatch.setenv("AGENT_WS_URL", f"ws://127.0.0.1:{port_holder['port']}")
        monkeypatch.setenv("CLAUDE_BIN", "printf")
        monkeypatch.setenv("CLAUDE_ARGS", "%s")
        monkeypatch.setenv("AGENT_AUTONOMOUS", "1")
        monkeypatch.setenv("AGENT_RECONNECT_DELAY", "0.05")

        agent_task = asyncio.create_task(agent_mod.main())
        await asyncio.wait_for(server_ready, timeout=10.0)
        activity = await asyncio.wait_for(got_activity, timeout=20.0)
        assert [frame["active_runs"] for frame in activity] == [1, 0]
        assert all(frame["type"] == "agent_activity" for frame in activity)
        assert all(frame["sig"] for frame in activity)
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

