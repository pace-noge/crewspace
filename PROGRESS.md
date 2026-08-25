# Crewspace — Session Progress (resume handoff)

Last updated: 2026-08-25 (WIB). M6.3 is complete on local `master` and pushed;
M6.4 is DONE (7/7) and pushed; M6.5 is DONE (7/7) and pushed; M6.6 is DONE
(7/7) and pushed. M6.7 is DONE (7/7) and pushed. M6.8 is IN PROGRESS (1/7). Verified
milestone commit for M6.8 slice 1 is ready.

## How to resume
1. `cd /home/bilal/Projects/Learning/python/crewspace`
2. `git log --oneline -10` to confirm history matches below.
3. `uv run pytest tests/test_inbox_projection.py -q` to confirm green (M6.8 bounded
   gate: 5 passed). M6.7 gate (24 passed) still green via tests/test_scorecard*.py +
   tests/test_benchmark_*.py; M6.6 gate (34 passed) via tests/test_pipeline*.py.
4. Pick up PLAN.md M6.8 — Operational inbox (IN PROGRESS, 1/7): next item 2
   (items dedupe deterministically and update/resolve with their source record —
   since item ids are source-derived, re-projection of a changed source record must
   update the same item in place, and a resolved source must drop/resolve it).

## Commits this session (newest first)
- `38c2e27` [verified] feat: transactional auth-scoped coding-run dispatch
- `item5` [verified] feat: reconcile disconnect/restart as interrupted runs
- `fe3f800` [verified] feat: cancellable coding runs with signed ack and subprocess termination
- `a1b3732` [verified] feat: persist bounded recent run output and restore on refresh
- `4b199be` [verified] feat: durable coding-run lifecycle and fail-closed transitions
- `6a78496` [verified] feat: complete remote workspace lifecycle
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
M6.4 — Typed execution events and unified event envelope — IN PROGRESS 2/7.
Slice 1 (versioned Envelope + typed catalog) and slice 2 (deterministic per-run
`RunSequencer`, `EventDedupeStore`, and `order_key` for replay/resume + dedupe)
are done in `src/crewspace/dto/events.py` + their contract tests; bounded gate 64
tests green, makemigrations --check clean, compileall clean, diff --check clean,
added-line security scan clean. Independent fail-closed review returned
BLOCKERS: none (verified in-process with executable checks; the delegated
reviewer subagent stalled last slice, so the verdict is in-process, not a
separate agent). Slice 1 also fixed the unanchored SafeId defect (pydantic
re.search accepted a substring of a traversal id). M6.3 is DONE (8/8, pushed
`c19eed7`); M6.2 DONE (7/7, `6a78496`); M6.1 DONE (6/6); M6.4 DONE (7/7). Next
milestone: M6.6 — Multi-agent delivery pipeline (DONE, 7/7). All slices landed:
versioned handoff contracts (handoffs.py), the deterministic state machine
(pipeline.py: input-gated stage starts, RetryPolicy, fail-closed terminal
FAILED, cancel blocks all transitions, immutable change-set evidence for the
reviewer via ChangeSetEvidence, duplicate-work guards, human-approval gate with
M6.5 fail-closed composition), the UI run-graph (pipeline_view.py +
pipeline_graph.html), and a real-repo end-to-end POC (test_pipeline_e2e_poc.py)
that drives planner->coder->reviewer->tester->human_approval to a verified,
immutable, human-approved change set linked to a real git HEAD. M6.6 is complete
and pushed. M6.7 — Agent evaluation and reliability scorecards is IN PROGRESS
(2/7): slice 1 shipped documented metric definitions (dto/metrics.py:
METRIC_DEFINITIONS with explicit denominator + privacy/retention note per
metric, plus MetricValue carrying numerator/denominator) and a pure deterministic
compute_scorecard(runs, tool_calls) over CodingRun + AgentToolCall records.
M6.7 is DONE (7/7): documented metric definitions + deterministic compute_scorecard
(slice 1); real-repository wiring via compute_team_scorecard + coding_runs.list_for_team
(slice 2); replayable benchmark fixtures isolated from production (slice 3); version
comparison without misleading cohort blends — BenchmarkSuite + compare_cohorts/
rank_cohorts (slice 4); regression thresholds that block rollout without
auto-promoting — evaluate_regression (slice 5); scorecard UI linking every aggregate
to inspectable supporting runs — build_scorecard_view + scorecard.html (slice 6);
seeded benchmark POC demonstrating version comparison + regression alert —
run_benchmark_poc (slice 7). M6.8 (Operational inbox) started: slice 1 shipped the
inbox item taxonomy + source-to-item projection rules as executable documentation
(application/inbox.py: INBOX_RULES + derive_inbox_id + build_inbox_item +
project_inbox_for_team). The inbox is a PROJECTION over source records/events, not a
second source of truth: item ids are derived deterministically from the source
(dedup by construction; items resolve with their source record) and projection is
team-scoped (cross-tenant records are dropped, no leakage). Next slice 2: items
dedupe deterministically and update/resolve with their source record.

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
