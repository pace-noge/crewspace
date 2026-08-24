# Crewspace — Session Progress (resume handoff)

Last updated: 2026-08-24 (WIB). All work below is COMMITTED and PUSHED to `master`,
synced with `origin/master`. Working tree is clean (no uncommitted changes).

## How to resume
1. `cd /home/bilal/Projects/Learning/python/crewspace`
2. `git log --oneline -8` to confirm history matches below.
3. `uv run pytest -q` to confirm green (current split-run baseline: 192 passed, 1 skipped).
4. Pick up `PLAN.md` M6.1 — Agent capability negotiation.

## Commits this session (newest first)
- `e8c6686` feat: stream remote agent output in chat
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
- Signed incremental remote-agent output: agents send correlated `agent_progress`
  deltas before the final `reply`; the app verifies identity/signature, validates
  each delta (non-empty, <=16 KiB), and routes it only to the active `(agent_id,
  message_id)` request.
- Chat renders temporary live output safely via `textContent`, retains the newest
  64 KiB, and removes it when the persisted final agent reply arrives.
- Progress broadcasts run independently of the final-reply timeout so a slow chat
  client cannot turn a valid final reply into a false timeout.
- Follow-up review hardening: progress is bounded per request (256 frames / 1 MiB),
  and a correlated `agent_progress_complete` frame clears only that request's live
  output on success, timeout, or disconnect. Cleanup is bounded/best-effort and
  cannot mask the final reply or original error.
- `examples/claude_code_agent.py` streams subprocess stdout line-by-line, while
  preserving the final captured reply; `docs/AGENT_PROTOCOL.md` documents the wire contract.

## POC verification (live, against running app)
- Ran the real app, real signed `claude_code_agent.py`, and a deterministic fake
  Claude subprocess that flushed `phase one` then `phase two`.
- Logged in over HTTP, opened the authenticated channel WebSocket, and sent
  `@planner stream the fake command`.
- Observed frame order: human message -> `typing` -> `agent_working` ->
  `agent_progress` (`phase one`) -> `agent_progress` (`phase two`) -> final persisted
  agent message (`phase one\nphase two`). Assertions passed.
- Note: mention uses the agent DISPLAY NAME (`@planner`), not the id (`agent_planner`).

## NEXT ACTION
M6.2 — Isolated worktrees and structured change sets is IN PROGRESS (3/7).
Authorized allocation and concurrent isolation are GREEN: operators configure
opaque repository IDs through `CREWSPACE_CODING_*`; canonical Git root/common-dir
identity is revalidated for each allocation; nested/swap paths are rejected; and
parallel runs receive distinct branches/checkouts. Twelve real-Git/settings tests
plus security regressions pass. Independent final review found no blockers;
structured change-set capture follows after the second verified M6.2 commit.

M6.1 — Agent capability negotiation is DONE (6/6). Verified behaviors: signed
versioned `hello`, explicit legacy profile, capability gates, additive external/
server-reserved capacity, reconnect-safe immediate request teardown, live sidebar
and management state, one-use connect claims, stale-socket rejection, and v1
session-bound monotonic sequencing. Final evidence: 107 focused tests passed; all
remaining suite files passed sequentially with one key-gated skip; compile,
diff, and added-line security scans passed; hardened live POC passed; independent
final review reported no blockers or suggestions. Commit evidence is recorded in
the M6 milestone log: verified implementation commit `e7aba78`.

## Test/run reminders (from prior sessions)
- `export CREWSPACE_DATABASE_URL=` persists across tool calls and overrides pytest fixtures'
  unique db_path (pydantic env > ctor arg) -> tests spuriously share one DB. Use inline
  `env VAR=...` per command or `unset` after a round-trip.
- A monolithic full-suite command again stalled after 51 tests in this session.
  All 192 tests were then run in three sequential file groups: 192 passed, 1 skipped.
  Verify affected WS/streaming tests individually or in sequential file groups.
- Builtin app-LLM agent (agent_crewspace) hits local gateway http://localhost:20128/v1 (model 'free')
  via .env CREWSPACE_LLM_*; if builtin replies fail, check the gateway is up.
