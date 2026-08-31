# Claude-Code remote agent

A **remote agent** for Crewspace that runs a `claude` subprocess for `@mention`s
and reports the output back to the chat thread, and that also executes governed
coding runs end to end.

It is a thin "give an agent a command and let it run" bridge: the app pushes a
`chat` frame (the prompt) → this process runs `claude` with that prompt → each
output line is sent as a signed `agent_progress` frame → when Claude exits, the
agent sends one signed `reply` frame with the captured output. Progress appears
live in the channel and is replaced by the persisted final reply in the human
message's thread. It additionally executes `coding_run` / `coding_workspace_action`
commands with a real Git worktree allocator, honours `coding_run_cancel`, and — when
`AGENT_AUTONOMOUS=1` — reacts to new cards and reports that autonomous external work
via signed `agent_activity` frames.

The agent is **self-healing**: if the socket drops it reconnects with a fresh,
one-use connect claim and a new session rather than exiting, and it never re-sends a
finished reply or change set after a reconnect.

## Why WebSocket is enough for long jobs

The agent holds **one long-lived WebSocket** to `ws://<host>/agents/ws`. A
Claude Code run can take minutes or hours; the socket stays open the whole time
and carries progress plus the final reply. There is no polling. The one thing
to respect is the app's **reply
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
export AGENT_CODING_REPOSITORIES='{"crewspace":"/srv/git/crewspace"}'
export AGENT_CODING_WORKTREE_ROOT="$HOME/.local/share/crewspace-agent/worktrees"
export AGENT_AUTONOMOUS=0        # 1 = react to new cards + publish agent_activity
export AGENT_RECONNECT_DELAY=1.0  # seconds between reconnect attempts (default 1.0)
export AGENT_MAX_CONCURRENCY=1    # negotiated max_concurrency (1–64)
```

The repository mapping is configured only on this execution host. Crewspace sends
opaque repository IDs and never receives or chooses these local filesystem paths.

The same private mapping owns workspace lifecycle operations. A
`coding_workspace_action` received on the authenticated socket contains only
repository ID, run ID, branch, and one of
`retain`, `cleanup`, or `discard`:

- `retain` is idempotent and prevents later cleanup or discard.
- `cleanup` removes only a clean workspace whose branch is already merged.
- `discard` may remove clean unmerged work only after explicit control-plane
  approval, but never removes a retained workspace.
- repeated successful removal returns `already_removed`.

Those lifecycle guarantees are process-local in this reference implementation.
Allocator ownership, retained markers, partial-cleanup state, and removal tombstones
are held in memory and are lost when the agent restarts. Cross-restart reconstruction
and durable idempotence are deferred to M6.3; operators must not treat a restart as
permission to discover or delete an unowned workspace.

Before every removal the example revalidates repository identity, allocator
ownership, workspace device/inode identity, current branch, and tracked/untracked
cleanliness. Local paths never appear in lifecycle result frames.

## Run

```bash
python claude_code_agent.py
```

Then in chat:

```
@coder refactor src/crewspace/api/connection.py to add a reset() method
```

The agent streams Claude Code output while it runs and posts the final result
back when it finishes. On connection it negotiates protocol v1 with `progress`
and `coding_workspace` support with one execution slot. Crewspace atomically
reserves that slot for each chat or coding request; autonomous agents may
additionally publish signed `agent_activity`
updates for work they start outside Crewspace.
The v1 acknowledgement installs a connection-scoped session id; the example
automatically signs it and a monotonically increasing sequence into every later
progress/reply frame so captured actions cannot be replayed after reconnect.

## How it relates to the protocol

- Builds the signed connect claim (`Authorization: Bearer *** — see
  `docs/AGENT_PROTOCOL.md` §3.
- Receives `chat` frames the app pushes on `@mention`; sends signed
  `agent_progress` frames followed by a signed `reply`.
- Receives path-free `coding_run` and `coding_workspace_action` frames; performs
  Git/worktree operations on this execution host and returns signed correlated
  change-set or lifecycle result/failure frames.
- Signs every outbound frame (Ed25519, canonical JSON) so the app verifies it
  and records the action under the agent's identity.

This example does **not** call the app's board tools (create_card, etc.). For an
agent that acts on the board, see the sibling `llm_agent.py`, which uses an LLM
to decide tool calls. This file is the subprocess-execution counterpart.
