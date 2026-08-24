"""Agent-side cancellation: terminate the tracked subprocess and ack signed."""
from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

import pytest

# The example agent is a standalone script; import it as a module by path.
EXAMPLE = Path(__file__).resolve().parents[1] / "examples" / "claude_code_agent.py"
sys.path.insert(0, str(EXAMPLE.parent))
import importlib.util

spec = importlib.util.spec_from_file_location("claude_code_agent_example", EXAMPLE)
agent_mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(agent_mod)


class _FakeSigner:
    """Minimal signer stub: returns the frame unchanged with a dummy sig."""

    def sign_frame(self, frame: dict) -> dict:
        f = dict(frame)
        f["sig"] = "test-sig"
        return f


class _FakeWebSocket:
    def __init__(self) -> None:
        self.sent: list[dict] = []

    async def send(self, payload) -> None:
        self.sent.append(json.loads(payload) if isinstance(payload, str) else payload)


@pytest.mark.asyncio
async def test_agent_cancel_terminates_tracked_subprocess_and_acks():
    sleep_bin = "sleep" if os.name != "nt" else "timeout"
    cmd = [sleep_bin, "30"] if os.name != "nt" else ["timeout", "30"]
    proc = await asyncio.create_subprocess_exec(*cmd)
    active_procs = {"run_x": proc}

    ws = _FakeWebSocket()
    frame = {"type": "coding_run_cancel", "request_id": "req_x", "run_id": "run_x"}
    await agent_mod._handle_coding_run_cancel(active_procs, frame, _FakeSigner(), ws.send)

    # Subprocess was terminated.
    assert proc.returncode is not None
    # Ack was sent, signed, and reports the cancelled run.
    assert len(ws.sent) == 1
    import json

    ack = ws.sent[0]
    assert ack["type"] == "coding_run_ack"
    assert ack["run_id"] == "run_x"
    assert ack["status"] == "cancelled"
    assert ack["sig"] == "test-sig"
    # The tracked subprocess is gone (registry is cleared by _run_claude on wait-return).


@pytest.mark.asyncio
async def test_agent_cancel_is_idempotent_without_live_proc():
    ws = _FakeWebSocket()
    frame = {"type": "coding_run_cancel", "request_id": "req_y", "run_id": "run_missing"}
    await agent_mod._handle_coding_run_cancel({}, frame, _FakeSigner(), ws.send)

    import json

    ack = ws.sent[0]
    assert ack["status"] == "cancelled"
    assert ack["run_id"] == "run_missing"
