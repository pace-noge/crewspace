"""Claude-Code remote agent (clone-and-run reference).

This is a *remote agent* for Crewspace: a separate process on its own machine
that dials INTO the app over the signed WebSocket protocol (Buzz model — the
agent connects, not the app). When someone @mentions it in chat, the app pushes
a ``chat`` frame with the message as the prompt. This agent runs that prompt as a
``claude`` subprocess, streams signed ``agent_progress`` frames line-by-line,
and sends one signed final ``reply`` frame with the captured output. It also
executes governed coding runs (``coding_run``), honours cancellation
(``coding_run_cancel``), applies safe workspace lifecycle commands
(``coding_workspace_action``), and — when ``AGENT_AUTONOMOUS=1`` is set — reacts
to new cards (``card_created``) by running work of its own and reporting that
autonomous external work via signed ``agent_activity`` frames.

WebSocket is the right transport for long jobs: it is one long-lived,
bidirectional connection that stays open for the whole subprocess run (minutes to
hours). There is no polling and no inherent time limit. The only thing to watch is
the app's reply timeout (``CREWSPACE_AGENT_REPLY_TIMEOUT``, default 1800s) — keep
long Claude runs under that, or raise it.

The agent is self-healing on transport failures: if the socket drops, it
reconnects (building a fresh, one-use connect claim and re-negotiating a new
session) instead of exiting, and it tracks completed request/message ids so a
reconnect never re-sends a finished reply or change set.

Your agent identity (Ed25519 private key) lives ONLY in this process's
environment; the app never sees it. Every frame we send is signed.

Run:
    pip install websockets cryptography
    export AGENT_PRIV="<base64url raw 32-byte private key from the register page>"
    export AGENT_ID="agent_coder"
    export AGENT_WS_URL="ws://127.0.0.1:8000/agents/ws"
    export CLAUDE_BIN="claude"                 # path to the Claude Code CLI
    export CLAUDE_ARGS="--print --verbose"       # forwarded to `claude` (optional)
    export AGENT_AUTONOMOUS=0                    # 1 to react to cards + report activity
    python claude_code_agent.py

Then in the app's chat:  @coder refactor src/crewspace/api/connection.py to add a reset() method
and the agent will run Claude Code with that prompt and post the result back.

This example deliberately does NOT use the app's tool-execution path (create_card,
etc.) for chat. It is a thin "prompt -> subprocess -> reply" bridge plus a governed
coding-run executor. If you want the agent to act on the board as a chat participant,
mirror the tool-calling pattern in llm_agent.py instead.
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
# Runtime state that survives reconnects
# --------------------------------------------------------------------------
class AgentRuntime:
    """State shared across reconnect attempts.

    Held here (not local to one connection) so a dropped socket that reconnects
    does not lose track of in-flight subprocesses, running tasks, the autonomous
    work count, or the completed request/message ids used for duplicate
    suppression.
    """

    def __init__(self, max_concurrency: int = 1) -> None:
        self.max_concurrency = max_concurrency
        self.active_procs: dict[str, asyncio.subprocess.Process] = {}
        self.running_tasks: dict[str, asyncio.Task] = {}
        self.autonomous_runs = 0
        # Bumped once per connection. A coding run started on connection N must
        # not deliver its change set on connection N+1's socket/session — the
        # app already reconciled the run on disconnect and would reject (or we
        # would sign a frame with the wrong session). Captured at launch.
        self.generation = 0
        # Idempotence across reconnects: once a request/message is answered we
        # never answer it again, so a re-negotiated session cannot duplicate work.
        # terminal_* are the authoritative dedup sets (a terminal run/request can
        # never execute or emit again); completed_* back them for reply tracking.
        self.completed_message_ids: set[str] = set()
        self.in_flight_message_ids: set[str] = set()
        self.terminal_run_ids: set[str] = set()
        self.terminal_request_ids: set[str] = set()
        self.in_flight_run_ids: set[str] = set()
        self.cancelled_run_ids: set[str] = set()
        # Background tasks that must outlive a single pump iteration but still be
        # cancelled when the owning connection is replaced (generation bump).
        self.background_tasks: set[asyncio.Task] = set()

    def add_autonomous(self, delta: int) -> int:
        self.autonomous_runs = max(0, min(self.max_concurrency, self.autonomous_runs + delta))
        return self.autonomous_runs

    def claim_coding_run(self, run_id: str, request_id: str) -> bool:
        """Atomically claim an inbound coding_run.

        Returns True the first time a run is claimed; False if the run is already
        terminal (success/failure/cancel) or already in flight — so a replayed or
        duplicate frame can NEVER double-execute or double-emit a terminal frame.
        """
        if run_id in self.terminal_run_ids or run_id in self.in_flight_run_ids:
            return False
        self.in_flight_run_ids.add(run_id)
        return True

    def add_terminal(self, run_id: str, request_id: str) -> None:
        """Record a terminal outcome for a run/request regardless of branch
        (success, failure, or cancellation). After this the run can never execute
        or emit again, and in-flight state is cleared.

        NOTE: does NOT pop running_tasks — that is the responsibility of the
        caller (finish_coding_run / _handle_coding_run_cancel) which may need
        the task handle for cancellation or capture before clearing."""
        self.terminal_run_ids.add(run_id)
        self.terminal_request_ids.add(request_id)
        self.in_flight_run_ids.discard(run_id)

    def mark_cancelled(self, run_id: str) -> None:
        self.cancelled_run_ids.add(run_id)

    def is_cancelled(self, run_id: str) -> bool:
        return run_id in self.cancelled_run_ids

    def is_terminal(self, run_id: str) -> bool:
        return run_id in self.terminal_run_ids

    def request_is_terminal(self, request_id: str) -> bool:
        return request_id in self.terminal_request_ids

    def recycle_generation(self) -> None:
        """When a new connection opens (generation bump), stop work started on
        the old connection. Those tasks hold a stale socket/session and cannot
        deliver; cancelling them avoids wasted subprocess work and leaks. The
        per-task `gen != runtime.generation` guard already blocks any send. Any
        in-flight run is forced terminal so a replay can never re-execute it."""
        for t in tuple(self.background_tasks):
            if not t.done():
                t.cancel()
            self.background_tasks.discard(t)
        for run_id in list(self.in_flight_run_ids):
            task = self.running_tasks.get(run_id)
            if task is not None and not task.done():
                task.cancel()
            self.terminal_run_ids.add(run_id)
            self.in_flight_run_ids.discard(run_id)
            self.running_tasks.pop(run_id, None)


# Explicit boolean parse (truthiness bug: os.environ.get("0") is truthy, so
# "AGENT_AUTONOMOUS=0" silently ENABLED autonomous work). Only accepted true
# values enable it; everything else, including the documented default "0",
# disables.
_TRUTHY = {"1", "true", "yes", "on"}


def autonomous_enabled() -> bool:
    return os.environ.get("AGENT_AUTONOMOUS", "0").strip().lower() in _TRUTHY


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
    runtime: AgentRuntime,
    frame: dict,
    signer: "Signer",
    send,
) -> None:
    """Terminate the (sub)process for a run, cancel its task, and record terminal.

    Idempotent: if no live subprocess/task is tracked for the run id we still
    send the signed acknowledgement so the control plane's cancel is always
    answered. Critically, the run is marked cancelled + terminal BEFORE any task
    completes, so `finish_coding_run` can never emit a change set after the
    cancellation ack (the protocol says to stop sending further change sets).
    """
    run_id = frame.get("run_id", "")
    request_id = frame.get("request_id", "")
    runtime.mark_cancelled(run_id)
    runtime.add_terminal(run_id, request_id)

    proc = runtime.active_procs.get(run_id)
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

    task = runtime.running_tasks.get(run_id)
    if task is not None and not task.done():
        task.cancel()

    ack = signer.sign_frame(
        {
            "type": "coding_run_ack",
            "request_id": request_id,
            "run_id": run_id,
            "status": "cancelled",
        }
    )
    await send(ack)
    print(f"[agent] cancelled run {run_id}", flush=True)


async def _publish_activity(
    runtime: AgentRuntime, signer: "Signer", send
) -> None:
    """Publish a signed agent_activity frame reporting autonomous external work.

    ``agent_activity`` tells the app how many slots this agent is using for work
    it started OUTSIDE a Crewspace-reserved request. The agent publishes the
    current count whenever it changes (start → N, finish → 0) so the app's live
    slot usage is accurate; sending is best-effort — a failure mid-disconnect is
    harmless because the next reconnect/report reconciles.
    """
    frame = signer.sign_frame(
        {"type": "agent_activity", "active_runs": runtime.autonomous_runs}
    )
    try:
        await send(frame)
        print(f"[agent] published agent_activity active_runs={runtime.autonomous_runs}", flush=True)
    except Exception as exc:
        print(f"[agent] agent_activity publish failed (ignored): {exc}", flush=True)


async def _handle_card_created(
    runtime: AgentRuntime,
    frame: dict,
    signer: "Signer",
    send,
) -> None:
    """React to a newly created card when autonomous mode is enabled.

    With AGENT_AUTONOMOUS=1 the agent treats a card as external work to pick up:
    it increments its reported activity (so the app knows it is busy), runs a
    short autonomous prompt, then decrements and reports back to 0. Without the
    flag (default, thin bridge) this is a no-op.
    """
    if not autonomous_enabled():
        return
    card = frame.get("card", {})
    title = card.get("title", "")
    runtime.add_autonomous(1)
    await _publish_activity(runtime, signer, send)
    try:
        await _run_claude(
            f"New card '{title}' was created. Briefly note what you will do.",
            active_procs=runtime.active_procs,
        )
    finally:
        runtime.add_autonomous(-1)
        await _publish_activity(runtime, signer, send)


async def _run_connection(
    ws,
    signer: "Signer",
    runtime: AgentRuntime,
    allocator: GitWorktreeAllocator,
    agent_id: str,
) -> None:
    """Negotiate a session and pump frames for one live connection.

    Raises on socket close so the caller's reconnect loop can open a new one.
    """
    runtime.generation += 1
    gen = runtime.generation
    runtime.recycle_generation()
    send_lock = asyncio.Lock()

    async def send(frame: dict) -> None:
        async with send_lock:
            await ws.send(json.dumps(frame))

    hello = signer.sign_frame(
        {
            "type": "hello",
            "protocol_version": 1,
            "agent_version": "crewspace-claude-code/1.1",
            "capabilities": ["progress", "coding_workspace", "cancellation"],
            "max_concurrency": runtime.max_concurrency,
        }
    )
    await send(hello)
    acknowledged = json.loads(await ws.recv())
    if acknowledged.get("type") != "hello_ack":
        raise RuntimeError(f"capability negotiation failed: {acknowledged}")
    signer.use_session(acknowledged["session_id"])
    print(f"[agent] connected as {agent_id}", flush=True)

    async def finish_coding_run(task: asyncio.Task, request_id: str, workspace, run_id: str) -> None:
        """Emit exactly one terminal frame for a non-cancelled run.

        Cancellation is recorded before the subprocess/task is stopped. The
        callback therefore checks cancellation BEFORE capturing or signing a
        change set, so no terminal completion can race after a cancel ack.
        """
        runtime.running_tasks.pop(run_id, None)
        if runtime.is_cancelled(run_id):
            runtime.add_terminal(run_id, request_id)
            return
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
            response = {
                "type": "coding_change_set",
                "request_id": request_id,
                "change_set": change_set.model_dump(mode="json"),
            }
        except asyncio.CancelledError:
            runtime.add_terminal(run_id, request_id)
            return
        except Exception as exc:
            response = {
                "type": "coding_run_failed",
                "request_id": request_id,
                "error": f"{type(exc).__name__}: {exc}"[-4096:],
            }
        # Mark terminal BEFORE sending so a duplicate/replayed coding_run cannot
        # interleave and execute while this frame is in flight.
        runtime.add_terminal(run_id, request_id)
        # If the connection that launched this run is gone, the app has already
        # reconciled the run to interrupted. Never sign with the new session and
        # write it to the dead old socket (cross-reconnect frames are rejected).
        if gen != runtime.generation:
            return
        await send(signer.sign_frame(response))

    async def run_chat(frame: dict, message_id: str) -> None:
        """Handle one chat @mention without blocking the receive pump."""
        try:
            text = frame["text"]
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
            # A reconnect may have replaced this generation; if so the response
            # would carry the new session but be written to the dead old socket.
            # Drop it — the app reconciled the message on disconnect.
            if gen != runtime.generation:
                return
            reply = signer.sign_frame(
                {"type": "reply", "message_id": message_id, "text": result}
            )
            await send(reply)
            runtime.completed_message_ids.add(message_id)
            print("[agent] replied", flush=True)
        finally:
            runtime.in_flight_message_ids.discard(message_id)
            for t in tuple(runtime.background_tasks):
                if t is not asyncio.current_task() and not t.done():
                    continue
                runtime.background_tasks.discard(t)

    async for raw in ws:
        frame = json.loads(raw)
        ftype = frame.get("type")

        if ftype == "chat":
            # The app pushed a chat message that @mentioned this agent. Run it as
            # a background task so the receive pump keeps observing socket
            # closure (a long silent subprocess must not postpone reconnection).
            message_id = frame.get("message_id", "")
            if message_id in runtime.completed_message_ids:
                continue  # re-negotiated session replaying a finished reply
            if message_id in runtime.in_flight_message_ids:
                continue  # already handling this message
            runtime.in_flight_message_ids.add(message_id)
            bt = asyncio.create_task(run_chat(frame, message_id))
            runtime.background_tasks.add(bt)

        elif ftype == "coding_run":
            request_id = frame["request_id"]
            run_id = frame["run_id"]
            # Reject replayed or duplicate runs atomically: a terminal or
            # already-running id never re-executes or double-emits.
            if not runtime.claim_coding_run(run_id, request_id):
                continue
            try:
                workspace = await asyncio.to_thread(
                    allocator.allocate,
                    repository_id=frame["repository_id"],
                    run_id=run_id,
                )
            except Exception as exc:
                runtime.add_terminal(run_id, request_id)
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
                    active_procs=runtime.active_procs,
                )
            )
            runtime.running_tasks[run_id] = task
            task.add_done_callback(
                lambda t, rid=request_id, ws_=workspace, rmid=run_id: asyncio.create_task(
                    finish_coding_run(t, rid, ws_, rmid)
                )
            )

        elif ftype == "coding_run_cancel":
            await _handle_coding_run_cancel(
                runtime, frame, signer, send
            )

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
            # Dispatch as a background task: autonomous subprocess work must not
            # block the receive pump from observing socket closure.
            bt = asyncio.create_task(
                _handle_card_created(runtime, frame, signer, send)
            )
            runtime.background_tasks.add(bt)

        elif ftype == "tool_result":
            # We don't request app tools in this example; nothing to do.
            pass

        elif ftype in {"hello_ack", "agent_activity_ack"}:
            pass


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
    max_concurrency = int(os.environ.get("AGENT_MAX_CONCURRENCY", "1"))
    runtime = AgentRuntime(max_concurrency=max_concurrency)
    # Bounded reconnect backoff (seconds), short by default so tests and quick
    # restarts recover fast; raise for production to avoid a tight loop.
    delay = float(os.environ.get("AGENT_RECONNECT_DELAY", "1.0"))

    while True:
        try:
            async with websockets.connect(
                ws_url,
                additional_headers={
                    "Authorization": "Bearer " + signer.connect_claim(agent_id)
                },
            ) as ws:
                await _run_connection(ws, signer, runtime, allocator, agent_id)
            # The connection loop ended (server closed the socket, cleanly or
            # not). This is not fatal for a remote daemon — fall through to the
            # reconnect path with a fresh claim + new session.
        except (websockets.exceptions.ConnectionClosed, OSError, ConnectionError) as exc:
            print(f"[agent] connection lost ({exc}); reconnecting in {delay:.1f}s", flush=True)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            print(f"[agent] fatal error: {exc}", flush=True)
            raise
        await asyncio.sleep(delay)


if __name__ == "__main__":
    required = ["AGENT_ID", "AGENT_PRIV", "AGENT_WS_URL"]
    missing = [v for v in required if not os.environ.get(v)]
    if missing:
        raise SystemExit(f"Missing env vars: {', '.join(missing)}")
    asyncio.run(main())
