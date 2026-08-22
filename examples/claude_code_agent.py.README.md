# Claude-Code remote agent

A minimal **remote agent** for Crewspace that turns an `@mention` into a
`claude` subprocess run and reports the output back to the chat thread.

It is the thinnest possible "give an agent a command and let it run" bridge:
the app pushes a `chat` frame (the prompt) → this process runs `claude` with
that prompt → when Claude exits, the agent sends one signed `reply` frame with
the captured output. The result appears in the chat thread under the human
message, exactly like any other agent reply.

## Why WebSocket is enough for long jobs

The agent holds **one long-lived WebSocket** to `ws://<host>/agents/ws`. A
Claude Code run can take minutes or hours; the socket stays open the whole time
and carries the final reply when it's ready. There is no polling and no size
limit on a single frame. The one thing to respect is the app's **reply
timeout** — `CREWSPACE_AGENT_REPLY_TIMEOUT` (default 1800s). If Claude runs
longer than that, the app gives up and posts "Agent did not respond". For
normal coding tasks 1800s is plenty; raise it if you need more.

## Prerequisites

- Python 3.12+
- The Crewspace app running
- `websockets` (already a project dependency) and `cryptography`
- The `claude` CLI on your `PATH` (or set `CLAUDE_BIN`)
- An **agent identity**: log in as an admin, open *Register agent*, copy the
  **private key** (shown once) and the agent id (e.g. `agent_coder`).

## Configure

```bash
export AGENT_PRIV="<base64url raw 32-byte private key from the register page>"
export AGENT_ID="agent_coder"
export AGENT_WS_URL="ws://127.0.0.1:8000/agents/ws"   # wss:// in production
export CLAUDE_BIN="claude"
export CLAUDE_ARGS="--print --verbose"                  # forwarded to `claude`
```

## Run

```bash
python claude_code_agent.py
```

Then in chat:

```
@coder refactor src/crewspace/api/connection.py to add a reset() method
```

The agent runs Claude Code and posts the result back when it finishes.

## How it relates to the protocol

- Builds the signed connect claim (`Authorization: Bearer *** — see
  `docs/AGENT_PROTOCOL.md` §3.
- Receives `chat` frames the app pushes on `@mention`; sends a signed `reply`.
- Signs every outbound frame (Ed25519, canonical JSON) so the app verifies it
  and records the action under the agent's identity.

This example does **not** call the app's board tools (create_card, etc.). For an
agent that acts on the board, see the sibling `llm_agent.py`, which uses an LLM
to decide tool calls. This file is the subprocess-execution counterpart.
