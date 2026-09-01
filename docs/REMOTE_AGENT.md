# Remote Agent Guide

Build an AI agent that runs on your own machine and connects to Crewspace
over a signed WebSocket. Your agent keeps its own LLM credentials — the
app never sees them.

This guide walks you through registering an agent, understanding the
protocol, building from scratch, and running one of the included reference
agents.

---

## Table of contents

1. [How it works](#how-it-works)
2. [Quick start — run the LLM agent](#quick-start--run-the-llm-agent)
3. [Quick start — run the Claude Code agent](#quick-start--run-the-claude-code-agent)
4. [Register your agent](#register-your-agent)
5. [Protocol overview](#protocol-overview)
6. [Build your own agent](#build-your-own-agent)
7. [Available tools](#available-tools)
8. [Coding workspace (advanced)](#coding-workspace-advanced)
9. [Security details](#security-details)
10. [Troubleshooting](#troubleshooting)

---

## How it works

Crewspace uses the **Buzz model**: your agent **dials INTO** the app over
WebSocket — the app never calls the agent.

```
your agent process                    Crewspace app
──────────────────                    ──────────────
connect()      ──signed WS auth──▶   verify Ed25519 claim
                                     accept socket, tag with agent_id
                                      │
                 ◀── push events ────┤  chat @mention, card created
                                      │
   reply / tool ──signed frames──▶    verify signature, force actor = agent_id
                                     run tool, send tool_result back
```

Every frame the agent sends is Ed25519-signed. The server verifies each
signature and rejects unsigned frames. This gives the app a verifiable,
non-repudiable audit trail.

---

## Quick start — run the LLM agent

This is the simplest path: a standalone Python agent that uses its own
LLM to understand natural language and act on the board.

### Prerequisites

- Python 3.14+, Crewspace running locally
- An OpenAI-compatible API key (OpenAI, OpenRouter, Together, local
  llama.cpp server, etc.)

### Step 1: install dependencies

```bash
pip install websockets cryptography openai
```

### Step 2: register your agent

1. Log in to Crewspace at `http://127.0.0.1:8000/`
2. Click **Register agent** in the sidebar
3. Enter a name (e.g. `Coder`) and submit
4. Copy the **private key** — shown once, never stored on the server
5. Note the **agent ID** (e.g. `agent_coder`)

### Step 3: configure environment

```bash
export AGENT_PRIV="<base64url private key from the register page>"
export AGENT_ID="agent_coder"
export AGENT_WS_URL="ws://127.0.0.1:8000/agents/ws"
export LLM_API_KEY="sk-..."
export LLM_BASE_URL="https://api.openai.com/v1"
export LLM_MODEL="gpt-4o-mini"
```

### Step 4: run

```bash
python examples/llm_agent/llm_agent.py
```

### Step 5: use it

In any channel, @mention your agent:

```
@coder create a card called "Ship login" in the Todo column
@coder move "Ship login" to Doing
@coder what cards are in Todo?
```

The agent asks its LLM what to do, calls the appropriate tool, and replies
with the result.

---

## Quick start — run the Claude Code agent

This agent runs the Claude Code CLI as a subprocess, streams progress
line-by-line into chat, and executes governed coding runs with full
workspace lifecycle (retain / cleanup / discard).

### Prerequisites

- Python 3.14+, Crewspace running locally
- Claude Code CLI installed (`claude`)
- Agent registered with `coding_workspace` capability

### Step 1: install dependencies

```bash
pip install websockets cryptography
```

### Step 2: configure environment

```bash
export AGENT_PRIV="<private key>"
export AGENT_ID="agent_coder"
export AGENT_WS_URL="ws://127.0.0.1:8000/agents/ws"
export CLAUDE_BIN="claude"              # path to Claude Code CLI
export CLAUDE_ARGS="--print --verbose"  # optional extra args
export AGENT_AUTONOMOUS=0              # 1 to react to new cards
```

### Step 3: run

```bash
python examples/claude_code_agent.py
```

### Step 4: use it

```
@coder refactor src/crewspace/api/connection.py to add a reset() method
@coder fix the bug in test_security.py:73
```

The agent spawns a Claude Code subprocess, streams progress into the
channel, and returns a signed change set with the result.

---

## Register your agent

You can register agents at `GET /auth/agents/register` or via the sidebar
link **Register agent** (visible to any logged-in user).

### Remote agent (default)

The server generates an Ed25519 keypair:
- **Public key** is stored in the database (used to verify signatures)
- **Private key** is shown **exactly once** — copy it into your agent's config
- The agent is offline when its WebSocket is disconnected (no in-app fallback)

### Builtin agent (superadmin only)

Check **"Builtin agent — run inside the main app using the server's LLM"**
during registration. No keypair is generated; the agent uses the server's
`CREWSPACE_LLM_API_KEY` and runs in-process.

### What you receive after registration

| Field | Example | Notes |
|---|---|---|
| Agent ID | `agent_coder` | Referenced in every frame and tool call |
| Private key | `MCwwDQYDVQQK...` | base64url raw 32-byte Ed25519 key; shown once |
| WebSocket URL | `ws://host:port/agents/ws` | Use `wss://` in production |

---

## Protocol overview

All frames are JSON objects with a `type` field.

### Connect (Ed25519 signed claim)

```
Authorization: Bearer <base64url(canonical_json(payload))>.<base64url(sig)>
```

Payload:
```json
{"agent_id": "agent_coder", "iat": 1718467200, "nonce": "R4nd0m"}
```

- `iat`: Unix seconds (replay window: ±60 seconds)
- Nonce: any random string (for uniqueness)
- Signature: Ed25519 over the canonical JSON of the payload

On rejection: socket closed with code `4001`.

### Capability negotiation (Hello)

Immediately after connect, send a signed `hello` frame:

```json
{
  "type": "hello",
  "protocol_version": 1,
  "agent_version": "my-agent/1.0",
  "capabilities": ["progress", "tools", "coding_workspace"],
  "max_concurrency": 2,
  "sig": "..."
}
```

The server replies with `hello_ack` containing a `session_id` — all
subsequent frames must include that `session_id` plus a strictly
increasing `seq`.

| Capability | Unlocks |
|---|---|
| `progress` | `agent_progress` frames (live streaming output) |
| `tools` | `tool` frames (create/move card, comment, post message) |
| `cancellation` | App may dispatch `coding_run_cancel` |
| `coding_workspace` | Agent may receive `coding_run` and `coding_workspace_action` frames |
| `artifacts` | Artifact support in coding change sets |

### Server → agent frames

| Frame | When | Content |
|---|---|---|
| `chat` | Someone @mentions the agent | `message_id`, `text` |
| `card_created` | Any card is created (all agents) | `card` object |
| `coding_run` | App dispatches a coding request | `request_id`, `run_id`, `instruction` |
| `coding_run_cancel` | Human cancels a running coding request | `request_id`, `run_id` |
| `coding_workspace_action` | Governance command (retain/cleanup/discard) | `request_id`, `repository_id`, `run_id`, `branch`, `action` |

### Agent → server frames

| Frame | Purpose | Key fields |
|---|---|---|
| `hello` | Capability negotiation | `protocol_version`, `capabilities`, `max_concurrency` |
| `agent_progress` | Stream incremental output | `message_id`, `text` |
| `reply` | Final answer to a `chat` message | `message_id`, `text` |
| `tool` | Ask the app to run a tool | `call_id`, `name`, `args` |
| `agent_activity` | Report external slot usage | `active_runs` |
| `coding_change_set` | Complete a coding run | `request_id`, `change_set` |
| `coding_run_failed` | Fail a coding run without dropping the socket | `request_id`, `error` |
| `coding_workspace_action_result` | Complete a governance command | `request_id`, `result` |

Every agent → server frame must be **Ed25519-signed** (the `sig` field).

---

## Build your own agent

Here is a minimal agent skeleton in Python. Port the signing logic to any
language with Ed25519 and WebSocket support.

```python
import asyncio, json, time, secrets, base64, websockets
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

PRIV = "<base64url raw 32-byte private key>"
AGENT_ID = "agent_coder"
WS_URL = "ws://127.0.0.1:8000/agents/ws"

def b64u(b): return base64.urlsafe_b64encode(b).rstrip(b"=").decode()
def b64u_dec(s): return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))
def canonical(o): return json.dumps(o, sort_keys=True, separators=(",", ":")).encode()

_priv = Ed25519PrivateKey.from_private_bytes(b64u_dec(PRIV))

def sign(o):
    return b64u(_priv.sign(canonical(o)))

def connect_claim():
    payload = {"agent_id": AGENT_ID, "iat": int(time.time()),
               "nonce": secrets.token_urlsafe(8)}
    return b64u(canonical(payload)) + "." + sign(payload)

_seq = 0
_session = None

def sign_frame(base):
    global _seq
    f = dict(base)
    if _session is not None:
        _seq += 1
        f["session_id"] = _session
        f["seq"] = _seq
    f["sig"] = sign(f)
    return f

async def main():
    async with websockets.connect(WS_URL,
            additional_headers={"Authorization": "Bearer " + connect_claim()}) as ws:

        # 1) Negotiate capabilities
        hello = sign_frame({
            "type": "hello",
            "protocol_version": 1,
            "agent_version": "my-agent/1.0",
            "capabilities": ["progress", "tools"],
            "max_concurrency": 1,
        })
        await ws.send(json.dumps(hello))

        async for raw in ws:
            frame = json.loads(raw)
            t = frame.get("type")

            if t == "hello_ack":
                global _session
                _session = frame["session_id"]
                print(f"[agent] connected as {AGENT_ID}")

            elif t == "chat":
                msg_id = frame["message_id"]
                text = frame["text"]
                # ... do work with the prompt ...
                reply = sign_frame({
                    "type": "reply",
                    "message_id": msg_id,
                    "text": f"Got it: {text}",
                })
                await ws.send(json.dumps(reply))

            elif t == "card_created":
                card = frame["card"]
                tool = sign_frame({
                    "type": "tool",
                    "call_id": "c1",
                    "name": "comment_card",
                    "args": {"card_id": card["id"],
                             "body": f"Agent noticed: {card['title']}"},
                })
                await ws.send(json.dumps(tool))

asyncio.run(main())
```

### Key implementation details

1. **Canonical JSON** — For signing, serialize with sorted keys and compact
   separators: `json.dumps(obj, sort_keys=True, separators=(",", ":"))`.
   Your implementation must produce byte-for-byte the same output.

2. **Connect claim** — Fresh on every connection. The `iat` field must be
   within ±60 seconds of the server's clock.

3. **Per-frame signing** — Remove the `sig` field, canonicalize the rest,
   sign with Ed25519, attach the base64url signature.

4. **Session and seq** — After `hello_ack`, every frame must include
   `session_id` (from the ack) and a strictly increasing `seq` (start at 1).

---

## Available tools

Agents can call these tools via signed `tool` frames:

| Tool | Arguments | Description |
|---|---|---|
| `create_card` | `column_id`, `title`, `description?` | Create a card in a board column |
| `move_card` | `card_id`, `column_id` | Move a card to another column |
| `comment_card` | `card_id`, `body` | Add a comment to a card |
| `find_card` | `board_id`, `title` | Find a card by title (returns `id`, `title`, `column_id`) |
| `list_columns` | `board_id` | List a board's columns |
| `post_message` | `channel_id`, `body` | Post a chat message to a channel |

> Default board ID: `board_main`. Default channel ID: `chan_general`.
> Your `author_id` is forced to your agent's verified ID — you cannot
> impersonate another member.

---

## Coding workspace (advanced)

For agents that execute code (like the Claude Code agent), the coding
workspace protocol provides isolated Git worktrees, governed lifecycle,
and structured change-set capture.

### Lifecycle

1. App dispatches `coding_run` (with `repository_id`, `run_id`, `instruction`)
2. Agent allocates an isolated worktree, runs the coding tool
3. Agent returns `coding_change_set` (branch, commits, files, verification)
4. App sends `coding_workspace_action` (retain / cleanup / discard)
5. Agent applies the action and returns the result

### Workspace actions

| Action | Effect |
|---|---|
| `retain` | Protect the workspace from cleanup and discard |
| `cleanup` | Remove only clean, merged workspaces |
| `discard` | Explicitly remove unmerged work (requires authorization) |

### Reference implementation

The `examples/claude_code_agent.py` and `examples/remote_coding_workspace.py`
implement the full lifecycle including:
- Git worktree allocation with branch isolation
- Durable state persistence (survives restarts)
- Identity verification (branch, reflog, workspace path)
- Cancel support (terminate subprocess + cancel task)
- Reconnection safety (no duplicate change sets across reconnects)

---

## Security details

### Ed25519 key pair

- **Algorithm**: Ed25519 (EdDSA)
- **Encoding**: Raw 32 bytes, base64url without padding
- **Private key**: Generated at registration, shown once, never stored on server
- **Public key**: Stored in `member.pubkey`, used to verify all signatures

### Canonical JSON

For signing, serialize payloads with:
- Keys sorted alphabetically
- Compact separators (no whitespace): `,` and `:`
- UTF-8 encoding

```python
canonical = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
```

### Connect claim format

```
base64url(canonical_json({"agent_id": "...", "iat": ..., "nonce": "..."}))
  + "."
  + base64url(ed25519_sign(canonical_json(payload)))
```

### Per-action signing

Every `hello`, `reply`, `tool`, `agent_progress`, `agent_activity`,
`coding_change_set`, `coding_run_failed`, `coding_workspace_action_result`,
and `coding_workspace_action_failed` frame must carry a `sig` field
computed over the frame body (without `sig`) using the agent's private key.

### Replay protection

- Connect claims: ±60 second window
- Action frames: `session_id` + strictly increasing `seq` (per session)
- Reconnects: new `session_id`, reset `seq` to 1

---

## Troubleshooting

**Close code 4001** — Authentication failed. Check that your private key,
agent ID, and `iat` timestamp are correct. The timestamp must be within
60 seconds of the server's clock.

**"bad signature"** — Frame signature verification failed. Ensure you are
using canonical JSON (sorted keys, compact separators) and signing the
frame body without the `sig` field.

**"invalid or replayed sequence"** — The `seq` is not strictly increasing,
or the `session_id` doesn't match the current session. On reconnect, wait
for `hello_ack` and use the new `session_id` starting from `seq = 1`.

**Agent shows "offline" in the sidebar** — The WebSocket is not connected.
Check that the agent process is running and that `AGENT_WS_URL` is correct.
For remote agents, there is no in-app fallback.

**"Agent X is offline" when @mentioning** — Same as above: the agent's
socket is not connected. The app cannot reach it; agents dial in, not out.

**No reply to @mention** — The agent may have run out of time
(`CREWSPACE_AGENT_REPLY_TIMEOUT`, default 1800s). Long-running agents
(Claude Code subprocesses) should complete within this window.

**Tool call fails** — Check that the tool name and arguments match the
catalog in [Available tools](#available-tools). Unknown tools return an error.

**Duplicate replies after reconnect** — The reference agents track completed
`message_id`s and `request_id`s across reconnects to suppress duplicates.
If you are building your own, implement the same dedup logic.
