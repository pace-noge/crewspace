"""
Claude-Code remote agent (clone-and-run reference).

This is a *remote agent* for Crewspace: a separate process on its own machine
that dials INTO the app over the signed WebSocket protocol (Buzz model — the
agent connects, not the app). When someone @mentions it in chat, the app pushes
a ``chat`` frame with the message as the prompt. This agent runs that prompt as a
``claude`` subprocess, streams signed ``agent_progress`` frames line-by-line,
and sends one signed final ``reply`` frame with the captured output.

WebSocket is the right transport for long jobs: it is one long-lived,
bidirectional connection that stays open for the whole subprocess run (minutes to
hours). There is no polling and no inherent time limit. The only thing to watch is
the app's reply timeout (``CREWSPACE_AGENT_REPLY_TIMEOUT``, default 1800s) — keep
long Claude runs under that, or raise it.

Your agent identity (Ed25519 private key) lives ONLY in this process's
environment; the app never sees it. Every frame we send is signed.

Run:
    pip install websockets
    export AGENT_PRIV="<base64url raw 32-byte private key from the register page>"
    export AGENT_ID="agent_coder"
    export AGENT_WS_URL="ws://127.0.0.1:8000/agents/ws"
    export CLAUDE_BIN="claude"                 # path to the Claude Code CLI
    export CLAUDE_ARGS="--print --verbose"       # forwarded to `claude` (optional)
    python claude_code_agent.py

Then in the app's chat:  @coder refactor src/crewspace/api/connection.py to add a reset() method
and the agent will run Claude Code with that prompt and post the result back.

This example deliberately does NOT use the app's tool-execution path (create_card,
etc.). It is a thin "prompt -> subprocess -> reply" bridge. If you want the agent to
act on the board, mirror the tool-calling pattern in llm_agent.py instead.
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
import secrets
import time
from collections.abc import Awaitable, Callable
from pathlib import Path

import websockets
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from crewspace.dto.change_sets import VerificationResultDTO

try:
    from examples.remote_coding_workspace import GitWorktreeAllocator
except ModuleNotFoundError:  # direct `python examples/claude_code_agent.py`
    from remote_coding_workspace import GitWorktreeAllocator


# --------------------------------------------------------------------------
# Protocol helpers (mirror docs/AGENT_PROTOCOL.md §3 exactly)
# --------------------------------------------------------------------------
def _b64u(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def _b64u_dec(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def _canonical(obj: dict) -> bytes:
    # Deterministic JSON: sorted keys, compact separators. Used for ALL signatures.
    return json.dumps(obj, sort_keys=True, separators=(",", ":")).encode("utf-8")


class Signer:
    def __init__(self, priv_b64u: str) -> None:
        self._priv = Ed25519PrivateKey.from_private_bytes(_b64u_dec(priv_b64u))
        self._session_id: str | None = None
        self._seq = 0

    def sign(self, obj: dict) -> str:
        """Ed25519-sign canonical(obj); return base64url signature."""
        return _b64u(self._priv.sign(_canonical(obj)))

    def connect_claim(self, agent_id: str) -> str:
        """Build the signed connect token: base64url(json) + '.' + sig."""
        payload = {
            "agent_id": agent_id,
            "iat": int(time.time()),
            "nonce": secrets.token_urlsafe(8),
        }
        return _b64u(_canonical(payload)) + "." + self.sign(payload)

    def sign_frame(self, frame: dict) -> dict:
        """Return a copy of the frame with a `sig` field attached."""
        f = dict(frame)
        if self._session_id is not None and frame.get("type") != "hello":
            self._seq += 1
            f["session_id"] = self._session_id
            f["seq"] = self._seq
        f["sig"] = self.sign({k: v for k, v in f.items() if k != "sig"})
        return f

    def use_session(self, session_id: str) -> None:
        self._session_id = session_id
        self._seq = 0


# --------------------------------------------------------------------------
# Agent loop
# --------------------------------------------------------------------------
async def _run_claude(
    prompt: str,
    on_progress: Callable[[str], Awaitable[None]] | None = None,
    *,
    cwd: Path | None = None,
    run_id: str | None = None,
    active_procs: dict[str, asyncio.subprocess.Process] | None = None,
) -> str:
    """Run `claude <args> <prompt>` and return its combined stdout.

    Streams progress via on_progress. When run_id/active_procs are supplied the
    subprocess is registered (and cleared on completion) so a concurrent
    coding_run_cancel can terminate it mid-run. The agent loop runs this as a
    concurrent task and keeps the WebSocket frame pump alive meanwhile.
    """
    bin_path = os.environ.get("CLAUDE_BIN", "claude")
    extra_args = os.environ.get("CLAUDE_ARGS", "").split()
    cmd = [bin_path, *extra_args, prompt]
    print(f"[agent] running: {' '.join(cmd)}", flush=True)
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        cwd=cwd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    if run_id is not None and active_procs is not None:
        active_procs[run_id] = proc
    out_chunks: list[str] = []
    assert proc.stdout is not None
    async for line in proc.stdout:
        text = line.decode("utf-8", errors="replace")
        # Surface progress both locally and in the Crewspace channel.
        print(f"[claude] {text.rstrip()}", end="", flush=True)
        out_chunks.append(text)
        if on_progress is not None:
            await on_progress(text)
    await proc.wait()
    if run_id is not None and active_procs is not None:
        active_procs.pop(run_id, None)
    full = "".join(out_chunks).strip()
    if proc.returncode != 0:
        full = f"(claude exited {proc.returncode})\n{full}"
    # Keep the reply within reason; the app renders it as a chat message.
    return full[-8000:] if len(full) > 8000 else full


def _workspace_action_response(allocator: GitWorktreeAllocator, frame: dict) -> dict:
    status = allocator.apply_workspace_action(
        repository_id=frame["repository_id"],
        run_id=frame["run_id"],
        branch=frame["branch"],
        action=frame["action"],
    )
    return {
        "type": "coding_workspace_action_result",
        "request_id": frame["request_id"],
        "result": {
            "repository_id": frame["repository_id"],
            "run_id": frame["run_id"],
            "branch": frame["branch"],
            "action": frame["action"],
            "status": status,
        },
    }




async def _handle_coding_run_cancel(
    active_procs: dict[str, asyncio.subprocess.Process],
    frame: dict,
    signer: "Signer",
    send,
) -> None:
    """Terminate the subprocess for a run and acknowledge cancellation.

    Idempotent: if no live subprocess is tracked for the run id, we still send
    the signed acknowledgement so the control plane's cancel is always answered.
    """
    run_id = frame.get("run_id", "")
    proc = active_procs.get(run_id)
    if proc is not None and proc.returncode is None:
        try:
            proc.terminate()
            try:
                await asyncio.wait_for(proc.wait(), timeout=10.0)
            except asyncio.TimeoutError:
                proc.kill()
                await proc.wait()
        except ProcessLookupError:
            pass
    ack = signer.sign_frame(
        {
            "type": "coding_run_ack",
            "request_id": frame.get("request_id", ""),
            "run_id": run_id,
            "status": "cancelled",
        }
    )
    await send(ack)
    print(f"[agent] cancelled run {run_id}", flush=True)


async def main() -> None:
    agent_id = os.environ["AGENT_ID"]
    ws_url = os.environ["AGENT_WS_URL"]
    signer = Signer(os.environ["AGENT_PRIV"])
    repository_config = json.loads(
        os.environ.get("AGENT_CODING_REPOSITORIES", "{}")
    )
    if not isinstance(repository_config, dict):
        raise ValueError("AGENT_CODING_REPOSITORIES must be a JSON object")
    allocator = GitWorktreeAllocator(
        repositories={key: Path(value) for key, value in repository_config.items()},
        worktree_root=Path(
            os.environ.get(
                "AGENT_CODING_WORKTREE_ROOT",
                "~/.local/share/crewspace-agent/worktrees",
            )
        ),
    )

    active_procs: dict[str, asyncio.subprocess.Process] = {}

    async with websockets.connect(
        ws_url,
        additional_headers={"Authorization": "Bearer " + signer.connect_claim(agent_id)},
    ) as ws:
        print(f"[agent] connected as {agent_id}", flush=True)
        hello = signer.sign_frame(
            {
                "type": "hello",
                "protocol_version": 1,
                "agent_version": "crewspace-claude-code/1.0",
                "capabilities": ["progress", "coding_workspace", "cancellation"],
                "max_concurrency": 1,
            }
        )
        await ws.send(json.dumps(hello))
        acknowledged = json.loads(await ws.recv())
        if acknowledged.get("type") != "hello_ack":
            raise RuntimeError(f"capability negotiation failed: {acknowledged}")
        signer.use_session(acknowledged["session_id"])

        send_lock = asyncio.Lock()

        async def send(frame: dict) -> None:
            async with send_lock:
                await ws.send(json.dumps(frame))

        running_tasks: dict[str, asyncio.Task] = {}

        async def finish_coding_run(task: asyncio.Task, request_id: str, workspace, run_id: str) -> None:
            try:
                result = task.result()
                change_set = await asyncio.to_thread(
                    allocator.capture,
                    workspace,
                    verification=[
                        VerificationResultDTO(
                            name="claude-code",
                            status=(
                                "failed"
                                if result.startswith("(claude exited")
                                else "passed"
                            ),
                            summary=result[-2000:],
                        )
                    ],
                    artifact_paths=[],
                )
                response = signer.sign_frame(
                    {
                        "type": "coding_change_set",
                        "request_id": request_id,
                        "change_set": change_set.model_dump(mode="json"),
                    }
                )
            except asyncio.CancelledError:
                return
            except Exception as exc:
                response = signer.sign_frame(
                    {
                        "type": "coding_run_failed",
                        "request_id": request_id,
                        "error": f"{type(exc).__name__}: {exc}"[-4096:],
                    }
                )
            running_tasks.pop(run_id, None)
            await send(response)

        async for raw in ws:
            frame = json.loads(raw)
            ftype = frame.get("type")

            if ftype == "chat":
                # The app pushed a chat message that @mentioned this agent.
                text = frame["text"]
                message_id = frame["message_id"]
                print(f"[agent] prompt: {text}", flush=True)

                async def send_progress(delta: str) -> None:
                    progress = signer.sign_frame(
                        {
                            "type": "agent_progress",
                            "message_id": message_id,
                            "text": delta,
                        }
                    )
                    await send(progress)

                try:
                    result = await _run_claude(text, on_progress=send_progress)
                except Exception as exc:  # never leave the app waiting forever
                    result = f"⚠️ agent error: {exc}"
                reply = signer.sign_frame(
                    {"type": "reply", "message_id": message_id, "text": result}
                )
                await send(reply)

                print("[agent] replied", flush=True)

            elif ftype == "coding_run":
                request_id = frame["request_id"]
                run_id = frame["run_id"]
                try:
                    workspace = await asyncio.to_thread(
                        allocator.allocate,
                        repository_id=frame["repository_id"],
                        run_id=run_id,
                    )
                except Exception as exc:
                    await send(
                        signer.sign_frame(
                            {
                                "type": "coding_run_failed",
                                "request_id": request_id,
                                "error": f"{type(exc).__name__}: {exc}"[-4096:],
                            }
                        )
                    )
                    continue
                # Run the subprocess as a concurrent task so the frame pump keeps
                # reading and a coding_run_cancel can terminate it mid-run.
                task = asyncio.create_task(
                    _run_claude(
                        frame["instruction"],
                        cwd=workspace.path,
                        run_id=run_id,
                        active_procs=active_procs,
                    )
                )
                running_tasks[run_id] = task
                task.add_done_callback(
                    lambda t, rid=request_id, ws_=workspace, rmid=run_id: asyncio.create_task(
                        finish_coding_run(t, rid, ws_, rmid)
                    )
                )

            elif ftype == "coding_run_cancel":
                await _handle_coding_run_cancel(active_procs, frame, signer, send)

            elif ftype == "coding_workspace_action":
                try:
                    response = await asyncio.to_thread(
                        _workspace_action_response, allocator, frame
                    )
                except Exception as exc:
                    response = {
                        "type": "coding_workspace_action_failed",
                        "request_id": frame.get("request_id", ""),
                        "error": f"{type(exc).__name__}: {exc}"[-4096:],
                    }
                await send(signer.sign_frame(response))

            elif ftype == "card_created":
                # Fire-and-forget: a card was created elsewhere. Ignore for this bridge.
                pass

            elif ftype == "tool_result":
                # We don't request app tools in this example; nothing to do.
                pass

            elif ftype in {"hello_ack", "agent_activity_ack"}:
                pass


if __name__ == "__main__":
    required = ["AGENT_ID", "AGENT_PRIV", "AGENT_WS_URL"]
    missing = [v for v in required if not os.environ.get(v)]
    if missing:
        raise SystemExit(f"Missing env vars: {', '.join(missing)}")
    asyncio.run(main())
