# Crewspace — Session Progress (resume handoff)

Last updated: 2026-08-22 (WIB). All work below is COMMITTED and PUSHED to `master`,
synced with `origin/master`. Working tree is clean (no uncommitted changes).

## How to resume
1. `cd /home/bilal/Projects/Learning/python/crewspace`
2. `git log --oneline -8` to confirm history matches below.
3. `uv run pytest -q` to confirm green (baseline: 190 passed, 1 skipped).
4. Pick up "NEXT ACTION" (option A) below.

## Commits this session (newest first)
- `b259473` feat: claude-code remote agent example + configurable remote reply timeout
- `8f89a3f` feat: live agent presence on connect/disconnect
- `fa23033` feat: stream connected agent working state in chat
- `a4a5860` feat: add per-run audit export links to workflow detail UI
- `37a4b84` test: fix async-SQLite teardown stall via NullPool and clean WebSocket-state reset
- `91e4cb7` test: quiet benign async-SQLite connection GC warning
- `0de4054` feat: export workflow run audits
- `dbef41e` feat: stream in-process workflow progress

## What was built (all pushed)
- Test-harness reliability: NullPool + clean WebSocket-state reset (root-caused a real
  teardown stall, not the benign GC warning).
- In-process workflow progress streaming (live per-step over WebSocket).
- Workflow run audit export: API + JSON/CSV (0de4054) + export links in workflow detail UI (a4a5860).
- Connected remote-agent "working" state in chat: typing -> agent_working -> reply (fa23033).
- Live agent presence on connect/disconnect: sidebar status dots update via a global
  `GET /ws/presence` WebSocket (no page reload) (8f89a3f).
- Claude-Code remote agent EXAMPLE: `examples/claude_code_agent.py` (+ `examples/claude_code_agent.py.README.md`)
  connects over the signed WebSocket, and on `@mention` runs `claude <prompt>` as a subprocess,
  then sends one signed `reply` frame with captured output.
- `CREWSPACE_AGENT_REPLY_TIMEOUT` (config.py + registry.py, default 1800s) replaces the old
  20s `send_and_wait` default so long Claude runs aren't cut off.

## POC verification (live, against running app)
- Ran `claude_code_agent.py` with a fake `claude`. Logged in, opened channel WS, sent
  `@planner refactor the parser now`.
- Observed: human echo -> `typing` -> `agent_working` -> agent spawned subprocess -> replied
  in-thread with captured output. RC=0, "POC PASS".
- Note: mention uses the agent DISPLAY NAME (`@planner`), not the id (`agent_planner`).

## NEXT ACTION (approved by user, NOT started)
**Option A: stream a connected remote agent's INCREMENTAL output into chat live,
as it runs** (instead of only one final reply at the end).

Design anchors to reuse (read before coding):
- Server: `src/crewspace/api/routers/chat.py` (broadcast) and
  `src/crewspace/api/connection.py` (`AgentConnectionManager.send_and_wait`) dispatch the
  remote agent's reply.
- Client render: `src/crewspace/templates/chat.html`.
- Agent example: `examples/claude_code_agent.py` — `_run_claude` currently buffers stdout
  then sends ONE signed `reply` frame; change to flush stdout line-by-line as progress frames.

Likely implementation:
- App currently consumes only `reply` frames for remote agents in chat (no generic inbound
  progress frame type). Add a new inbound frame, e.g. `agent_progress` (agent_id, message_id,
  text/delta), have chat.py broadcast it, and chat.html render it as a live/incremental message.
- Keep the final signed `reply` for the completed result; progress frames carry interim output.
- Ensure `CREWSPACE_AGENT_REPLY_TIMEOUT` still covers the whole run (progress does not reset it).

TDD plan:
- Unit/integration test proving progress frames arrive BEFORE the final reply.
- Re-run the live POC (driver pattern in /tmp/poc_driver.py from the b259473 session) to show
  incremental frames in the channel.

## Test/run reminders (from prior sessions)
- `export CREWSPACE_DATABASE_URL=` persists across tool calls and overrides pytest fixtures'
  unique db_path (pydantic env > ctor arg) -> tests spuriously share one DB. Use inline
  `env VAR=...` per command or `unset` after a round-trip.
- Full suite can exceed shell time caps during teardown (pre-existing aiosqlite/TestClient leak,
  not a logic regression). Verify affected WS/streaming tests individually.
- Builtin app-LLM agent (agent_crewspace) hits local gateway http://localhost:20128/v1 (model 'free')
  via .env CREWSPACE_LLM_*; if builtin replies fail, check the gateway is up.
