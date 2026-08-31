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
import subprocess
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


def _git(path: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(path), *args],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _make_repository(tmp_path: Path) -> Path:
    repo = tmp_path / "repository"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.name", "Cancel E2E")
    _git(repo, "config", "user.email", "cancel-e2e@example.test")
    (repo / "README.md").write_text("seed\n")
    _git(repo, "add", "README.md")
    _git(repo, "commit", "-m", "seed")
    return repo


def _make_key_env() -> tuple[str, str]:
    priv = Ed25519PrivateKey.generate()
    priv_b64u = base64.urlsafe_b64encode(priv.private_bytes_raw()).rstrip(b"=").decode()
    pub = priv.public_key()
    pub_b64u = base64.urlsafe_b64encode(pub.public_bytes_raw()).rstrip(b"=").decode()
    return priv_b64u, pub_b64u


def _make_helper_script(tmp_path: Path) -> Path:
    """Create a tiny shell script that sleeps 30s ignoring extra args.

    The agent runs `[script, prompt]`, so a plain `sleep` CLI breaks when it
    receives a non-numeric prompt. This helper sleeps 30s regardless of args
    so the coding run is genuinely alive when cancel arrives.
    """
    script = tmp_path / "sleep_helper.sh"
    script.write_text("#!/bin/sh\nexec sleep 30\n")
    script.chmod(0o755)
    return script


async def _server(
    server_ready: asyncio.Future,
    got_ack: asyncio.Future,
    got_forbidden_terminal: asyncio.Future,
    post_cancel_quiet: asyncio.Future,
    port_holder: dict,
) -> None:
    import websockets

    async def handler(ws):
        port_holder["port"] = ws.local_address[1]
        raw = await ws.recv()
        hello = json.loads(raw)
        assert hello["type"] == "hello"
        await ws.send(json.dumps({"type": "hello_ack", "session_id": "sess-1"}))
        server_ready.set_result(True)
        dispatch = {
            "type": "coding_run",
            "request_id": "req_e2e",
            "repository_id": "repo_e2e",
            "run_id": "run_e2e",
            "instruction": "sleep 30",
        }
        await ws.send(json.dumps(dispatch))
        await asyncio.sleep(1.0)
        await ws.send(json.dumps({
            "type": "coding_run_cancel",
            "request_id": "req_e2e",
            "run_id": "run_e2e",
        }))
        # The agent must acknowledge cancellation and then remain terminal-quiet:
        # no later coding_change_set/coding_run_failed for the cancelled request.
        while not got_ack.done():
            msg = json.loads(await asyncio.wait_for(ws.recv(), timeout=20.0))
            if msg.get("type") in {"coding_change_set", "coding_run_failed"}:
                if not got_forbidden_terminal.done():
                    got_forbidden_terminal.set_result(msg)
            if msg.get("type") == "coding_run_ack" and msg.get("status") == "cancelled":
                got_ack.set_result(msg)
                break
        try:
            while True:
                msg = json.loads(await asyncio.wait_for(ws.recv(), timeout=1.0))
                if msg.get("type") in {"coding_change_set", "coding_run_failed"}:
                    if not got_forbidden_terminal.done():
                        got_forbidden_terminal.set_result(msg)
        except asyncio.TimeoutError:
            if not post_cancel_quiet.done():
                post_cancel_quiet.set_result(True)

    async with websockets.serve(handler, "127.0.0.1", 0) as srv:
        port_holder["port"] = srv.sockets[0].getsockname()[1]
        await asyncio.Event().wait()


@pytest.mark.asyncio
async def test_agent_cancel_mid_run_via_real_loop(monkeypatch, tmp_path: Path):
    if os.name == "nt":
        pytest.skip("posix subprocess semantics required")
    # Build a real repo so allocator.allocate succeeds — the test never actually
    # exercised the cancel-while-running path without this.
    repo = _make_repository(tmp_path)
    worktree_root = tmp_path / "worktrees"
    worktree_root.mkdir()
    # Use a shell helper that sleeps 30s and ignores extra args. A plain
    # `sleep` CLI breaks when it receives a non-numeric prompt argument.
    helper = _make_helper_script(tmp_path)

    priv_b64u, _ = _make_key_env()
    port_holder: dict = {}
    server_ready = asyncio.get_event_loop().create_future()
    got_ack = asyncio.get_event_loop().create_future()
    got_forbidden_terminal = asyncio.get_event_loop().create_future()
    post_cancel_quiet = asyncio.get_event_loop().create_future()

    server_task = asyncio.create_task(
        _server(server_ready, got_ack, got_forbidden_terminal, post_cancel_quiet, port_holder)
    )
    agent_task = None
    await asyncio.sleep(0.1)
    try:
        monkeypatch.setenv("AGENT_ID", "agent_cancel_e2e")
        monkeypatch.setenv("AGENT_PRIV", priv_b64u)
        monkeypatch.setenv("AGENT_WS_URL", f"ws://127.0.0.1:{port_holder['port']}")
        # The helper script sleeps 30s and ignores the prompt argument, so the
        # run stays alive long enough for cancel to arrive mid-execution.
        monkeypatch.setenv("CLAUDE_BIN", str(helper))
        monkeypatch.setenv("CLAUDE_ARGS", "")
        monkeypatch.setenv("AGENT_CODING_REPOSITORIES", json.dumps({"repo_e2e": str(repo)}))
        monkeypatch.setenv("AGENT_CODING_WORKTREE_ROOT", str(worktree_root))

        agent_task = asyncio.create_task(agent_mod.main())
        await asyncio.wait_for(server_ready, timeout=10.0)
        ack = await asyncio.wait_for(got_ack, timeout=25.0)
        assert ack["status"] == "cancelled"
        assert ack["run_id"]
        assert ack["sig"]
        await asyncio.wait_for(post_cancel_quiet, timeout=5.0)
        assert not got_forbidden_terminal.done(), (
            "cancelled run emitted a terminal change set/failure after cancel"
        )
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
