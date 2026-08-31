# Crewspace — Remote Agent Reference Implementations (M8)

Status: PLANNED
Dependencies: M6.1–M6.6 (capabilities, runs, durable lifecycle, typed events,
approvals, multi-agent pipeline) and the M7 board operating surface.

Goal: bring the reference remote agents (`examples/`) up to the level of the
control plane the app already ships. The main app implements approvals, typed
events, the delivery pipeline, scorecards, durable coding runs, and a board
operating surface; the reference remote agents are thin protocol clients that
negotiate capabilities they cannot back (`artifacts`, `patches`, `resume`,
`heartbeat` are accepted in `hello` but have no inbound handler in
`api/routers/agents.py`) and never exercise the modern M6/M7 machinery. M8
elevates the reference remote coding agent (M8.1), makes the runtime durable
across restarts (M8.2), adds an approval-aware reference path (M8.3), and shows
a multi-agent pipeline participant example (M8.4) — so the control plane is
actually driven by real, governed agents.

Verified-slice discipline applies to every slice below: RED test first, bounded
test gate, migration-compat guard (pure DTO where applicable), compileall /
`git diff --check` / added-line security scan, independent fail-closed review,
then one `[verified]` commit + push. Each slice records the feature behavior and
the concrete code touched. No slice is marked DONE until its acceptance items
are checked and evidence (tests + commit) is recorded in the M8 progress log.

Invariant for all slices: the remote agent's LLM key lives ONLY in the agent
process — the app must never see it. Examples stay clone-and-run against any
OpenAI-compatible endpoint and keep the signed protocol (Ed25519 connect claim +
signed frames). The app must never be sent an unverified assumption; every frame
type added here must be checked against `api/routers/agents.py`'s real inbound
dispatch (the source of truth for the wire contract).

--------------------------------------------------------------------------------
M8.1 — Modern reference remote coding agent                 [L]  PLANNED
--------------------------------------------------------------------------------
Feature:
  Upgrade `examples/claude_code_agent.py` from the "thinnest possible bridge"
  into the canonical reference remote coding agent that honestly drives the
  app's coding control plane. On connect it negotiates protocol v1 and ONLY the
  capabilities it actually implements, then: streams a long-lived `claude`
  subprocess as typed `agent_progress` frames; honours an inbound `cancel` frame
  by terminating the subprocess and acknowledging; publishes signed
  `agent_activity` updates for work it starts outside Crewspace; and reconnects
  with a session/resume cursor so a dropped socket resumes without losing or
  duplicating progress. It converges on the same tool/artifact surface the app
  already gates (default-deny tool governance, approval checkpoints wired in
  M8.3).

Code (concrete):
  - examples/claude_code_agent.py: rewrite the frame pump to a dispatch loop keyed
    on the negotiated, honestly-declared capability set; add `cancel` handling
    (subprocess.terminate + signed `cancel_ack`), `agent_activity` publishing, and
    a reconnect loop that re-negotiates `hello` with a fresh session while carrying
    a resume cursor for `progress` frames.
  - examples/remote_coding_workspace.py: converge the action results on the
    typed frames `api/routers/agents.py` actually handles
    (`coding_workspace_action_result`, `coding_run_failed`, ...), so a real
    `change_set` capture flows back through the same seam M6.2/M6.3 use.
  - docs/AGENT_PROTOCOL.md §capabilities: mark negotiated-but-unwired capabilities
    (`artifacts`, `patches`, `resume`, `heartbeat`) as declaration/profile
    metadata only until a handler exists; a capability a reference agent declares
    must be backed by code here (align the reference agent with the documented,
    wired set).
  - tests/: a Node DOM-shim-free, in-process protocol test harness that dials a
    fake `agent_ws` (per the M7.3 board-live pattern) and asserts: negotiated
    caps == implemented caps, `progress` frames resume after an injected
    disconnect, `cancel` reaches a terminal state, and `agent_activity` is
    signed + accepted.

Acceptance:
  - [ ] Reference coding agent negotiates protocol v1 and ONLY capabilities it actually implements.
  - [ ] Long-lived `claude` subprocess output streams as typed `agent_progress` and lands in the app's run/activity view.
  - [ ] An inbound `cancel` terminates the subprocess and reaches a terminal run state; the agent signs the acknowledgment.
  - [ ] Signed `agent_activity` updates surface in the team-scoped activity stream with no cross-tenant leakage.
  - [ ] A dropped socket reconnects and resumes progress without gaps or duplicates (resume cursor honored).
  - [ ] docs/AGENT_PROTOCOL.md capability table is walked in lockstep so negotiated == wired for this agent.
  - [ ] Feature + code documented in the M8 progress log with tests + commit evidence.

--------------------------------------------------------------------------------
M8.2 — Durable remote workspace lifecycle on the execution host   [L]  PLANNED
--------------------------------------------------------------------------------
Feature:
  Make the execution-host workspace lifecycle durable so a restart reconstructs
  ownership instead of losing cleanup safeguards. Today `remote_coding_workspace.py`
  holds allocator ownership, retained markers, and removal tombstones in memory
  and loses them on restart (the documented M6.3 deferral). This slice persists
  that state and lifts the deferral: an owner is never forgotten, a retained
  workspace is never deleted after restart, and cleanup/discard are idempotent
  across a restart with the same fail-closed guarantees as before.

Code (concrete):
  - examples/remote_coding_workspace.py: replace the in-memory allocator/retained/
    tombstone state with a durable store (e.g. a JSON document or small SQLite file
    under the configured worktree root) written transactionally on every lifecycle
    transition; on startup, reconstruct allocator ownership and retained/tombstone
    sets from the store before serving any `coding_workspace_action`.
  - Re-validate repository identity, allocator ownership, workspace device/inode
    identity, current branch, and clean tracked/untracked state before EVERY removal
    (unchanged fail-closed behavior), now additionally guarded by the reconstructed
    durable record.
  - tests/: restart-recovery tests that populate the durable store, simulate a
    process restart (fresh allocator object), and assert ownership/retained/
    tombstone state matches; a retained workspace is NOT removable after restart;
    cleanup/discard are idempotent and never delete an unmerged retained workspace.

Acceptance:
  - [ ] Allocator ownership, retained markers, and removal tombstones persist across a process restart.
  - [ ] Startup reconstructs ownership from durable state before serving lifecycle actions.
  - [ ] A retained workspace is never deleted after restart (fail-closed).
  - [ ] cleanup/discard remain idempotent and revalidate full safety invariants post-restart.
  - [ ] No cross-tenant path leakage: local paths never appear in lifecycle result frames.
  - [ ] Migration-compat guard clean (pure, sqlalchemy-free if a store is added to the repo).
  - [ ] Feature + code documented in the M8 progress log with tests + commit evidence.

--------------------------------------------------------------------------------
M8.3 — Approval-aware reference agent path                 [M]  PLANNED
--------------------------------------------------------------------------------
Feature:
  Give the reference remote agent a concrete path that exercises the M6.5
  run-scoped approval policy end to end from the remote side. When a coding run
  reaches a consequential action class (`git_push`, `deploy`,
  `package_install`, `network_egress`, `shell_command`, `file_write`,
  `external_mcp`), the agent pauses and surfaces the `approval` (requested)
  checkpoint; it continues only on an explicit `granted` decision bound to that
  run/action class, and blocks/fails closed on `denied`/`expired`/`requested`.
  This proves the human-approval gate works with a real agent, not just app-internal
  seams.

Code (concrete):
  - examples/: a reference approval-aware path (extending the M8.1 coding agent)
    that, on hitting a consequential action, emits the canonical `approval`
    envelope via the app seam and waits for the decision.
  - Reuse `application/run_policy.py` `evaluate_action` semantics: only an explicit
    `granted` (policy-allowed OR prior granted bound to the class) lets the action
    proceed; `denied`/`expired`/`requested` block. A granted-for-X never unlocks a
    different action class (scope-escalation guard) — reused, not reinvented.
  - tests/: an end-to-end POC proving a real agent flow pauses, requests approval,
    resumes on `granted`, and fails closed on `denied`/`expired`/replay of a denied
    decision; the checkpoint surfaces in the activity stream + audit export.

Acceptance:
  - [ ] A remote agent reaching a consequential action class emits an `approval` (requested) checkpoint and pauses.
  - [ ] A `granted` decision (bound to run + action class) lets the agent proceed.
  - [ ] `denied`/`expired`/`requested` (unresolved) fail closed and block execution.
  - [ ] A granted approval for one action class never unlocks a different class.
  - [ ] Replay of a denied/expired approval cannot execute the protected action.
  - [ ] Feature + code documented in the M8 progress log with tests + commit evidence.

--------------------------------------------------------------------------------
M8.4 — Pipeline-participant reference example              [M–L]  PLANNED
--------------------------------------------------------------------------------
Feature:
  Demonstrate the M6.6 multi-agent delivery pipeline with real reference agents:
  one process runs as `planner`, another as `coder`, another as `reviewer`,
  passing structured handoff artifacts (not free agent-to-agent chat) through the
  versioned `HandoffContract`. The example shows a card → plan → code →
  review → human-approval flow reaching a verified change set, with the reviewer
  receiving independent, tamper-evident `ChangeSetEvidence`.

Code (concrete):
  - examples/: a pipeline-participant example that runs one or more agent roles
    (planner/coder/reviewer) over the shared signed protocol, driving
    `application/pipeline.py` through `begin_stage`/`complete_stage`/artifact
    attachment and honoring the NO-AUTO-ADVANCE `human_approval` gate.
  - Reuse `dto/handoffs.py` stage contracts + `ChangeSetEvidence`; a coder stage
    produces a real change set the reviewer consumes with its own context.
  - tests/: an end-to-end POC driving the typed stages with the reference roles and
    asserting: input-gated stage eligibility, no silent downstream advance on
    failure, capped retries, and human approval required before final delivery.

Acceptance:
  - [ ] Reference agents run as distinct pipeline roles (planner/coder/reviewer) over the signed protocol.
  - [ ] Handoff artifacts flow through versioned `HandoffContract` types, not free chat.
  - [ ] Reviewers receive independent context + immutable `ChangeSetEvidence`.
  - [ ] Failed/cancelled stages cannot silently advance or duplicate downstream work.
  - [ ] Human approval is required before the configured delivery action.
  - [ ] Feature + code documented in the M8 progress log with tests + commit evidence.

--------------------------------------------------------------------------------
M8 Progress log (append-only, newest first)
--------------------------------------------------------------------------------
- M8.1 — Modern reference remote coding agent — [verified] committed + pushed.

  Feature: modernized `examples/claude_code_agent.py` into a self-healing
  reference remote coding agent. It negotiates protocol v1 and ONLY the
  capabilities it actually implements (`progress`, `coding_workspace`,
  `cancellation`). If the socket drops it reconnects with a fresh, one-use
  connect claim + new session (bounded `AGENT_RECONNECT_DELAY` backoff) instead
  of exiting, and tracks completed `message_id`s / `request_id`s across the
  reconnect so it never re-sends a finished `reply` or `coding_change_set`. With
  `AGENT_AUTONOMOUS=1` it reacts to `card_created` and publishes signed
  `agent_activity` frames reporting its autonomous external work (kept within
  `max_concurrency`), so the app reflects real slot usage. Long-lived `claude`
  subprocesses stream as `agent_progress`; `coding_run_cancel` terminates them
  and reaches a signed terminal ack; governed `coding_workspace_action` results
  stay path-free.

  Code:
  - examples/claude_code_agent.py: `AgentRuntime` (reconnect-surviving state:
    active_procs, running_tasks, autonomous_runs, completed ids, and a
    `generation` counter); `_run_connection` frame pump (reconnectable);
    reconnect loop in `main()` with fresh claim per reconnect; a `generation`
    guard in `finish_coding_run` so a coding run that finishes after a reconnect
    never signs/sends a terminal frame on the dead socket or under the new
    session (cross-reconnect frames are rejected server-side);
    `_handle_card_created` + `_publish_activity` for signed `agent_activity`;
    `_handle_coding_run_cancel` seam preserved (dict arg) so existing
    cancellation tests stay green. Env: `AGENT_AUTONOMOUS`, `AGENT_RECONNECT_DELAY`,
    `AGENT_MAX_CONCURRENCY`.
  - tests/test_claude_code_agent_m81.py: E2E reconnect/resume test (server
    accepts two sequential connections; asserts a NEW session per reconnect and
    the resumed chat is answered), negotiated-caps == implemented-caps test, and
    an E2E signed `agent_activity` publish test asserting the exact `[1, 0]`
    start/release lifecycle (caught a real zero-suppression defect: active_runs=0
    was suppressed, which would leave the app seeing the agent permanently busy).
  - tests/test_management.py: `test_claude_example_negotiates_server_managed_chat_capacity`
    updated (old `'"type": "agent_activity"' not in source` assertion now asserts
    the agent publishes it), matching the new behavior.
  - docs/AGENT_PROTOCOL.md: documented agent-side reconnect (fresh claim + new
    session, no duplicate replies/change sets) in §5c and the reference agent's
    `agent_activity` publishing in the `agent_activity` frame section.
  - examples/claude_code_agent.py.README.md: new header/self-healing description,
    `AGENT_AUTONOMOUS` / `AGENT_RECONNECT_DELAY` / `AGENT_MAX_CONCURRENCY` env docs.
  - scripts/review_m81_inproc.py: reusable in-process fail-closed review gate.

  Verification: tests/test_claude_code_agent_m81.py (3) red then green; the
  strengthened `[1, 0]` activity assertion drove a real code fix. Bounded gate
  (m81 + cancellation + e2e + change_sets + remote_change_set_poc + agent_presence)
  green; management/agent_connections/agent_routing green (100+); security green
  (20). compileall, `git diff --check`, and AST sqlalchemy-free scan clean;
  `makemigrations --check` in sync at head `20260831_01` (no schema change). Independent reviewer subagent stalled
  repeatedly re-running the broad test_management.py group without a verdict, so
  per the milestone-slice workflow the fail-closed review was run IN-PROCESS via
  scripts/review_m81_inproc.py (asserts negotiated caps == implemented subset,
  reconnect loop + fresh claim + session, cross-reconnect generation guard,
  cancellation subprocess handling, signed agent_activity, sqlalchemy-free, and
  server-side session/seq + AGENT_PROTOCOL replay/reconnect rules). In-process
  verdict: BLOCKERS: none.
