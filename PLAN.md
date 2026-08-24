# Crewspace — Roadmap & Milestones

Goal: grow the learning slice into a real, multi-tenant "Slack-meets-Trello with
agents" where (1) agents run on a real LLM with tool use, (2) the app is itself
exposable to other agents via MCP, (3) there are multiple channels/threads, and
(4) persistence is Postgres.

Guiding principle (carried from the slice): **one Tool Registry, many fronts.**
Every capability (create card, move card, comment, post message, list board…) is
a single registered `Tool`. It is called by:
  - the LLM agent (function calling),
  - the MCP server (same definitions),
  - eventually the HTMX UI (already does this directly today).
This avoids the usual fork where the UI, the agent, and the API drift apart.

Legend: S ≈ <0.5 day · M ≈ 0.5–1.5 days · L ≈ 2+ days (solo, learning pace)

------------------------------------------------------------------------------
M0 — Tool Registry (keystone)                                  [S–M]
------------------------------------------------------------------------------
Why first: both the real LLM agent (M1) and MCP exposure (M2) need a stable,
typed set of callable tools. Build it once, use it everywhere.

Scope:
  - Define `Tool` = name + description + JSON-Schema input + async handler(ctx, **args) -> dict.
  - Register tools: create_card, move_card, comment_card, list_board,
    post_message, list_messages. Handlers wrap existing db.py functions.
  - `ToolRegistry` holds them; `agent.py` and `mcp_server.py` both consume it.
  - Keep `StubAgent` working by routing its regex commands to the same tools
    (proves the registry end-to-end before any LLM).

Acceptance:
  - `GET /tools` (dev/debug) lists all tools with their JSON schemas.
  - StubAgent `@planner new card "X" in Todo` still works, now via the registry.
  - A unit test asserts each tool handler runs against the test DB.

Files: new `tools.py`, `tools_registry.py`; extend `agent.py`; tests.

------------------------------------------------------------------------------
M1 — Real LLM Agent (tool use)                                [M–L]  DONE
------------------------------------------------------------------------------
Scope:
  - Implement `LLMAgent` satisfying `AgentProvider` (same interface as StubAgent).
  - Provider injected by `get_agent()` (env switch: CREWSPACE_AGENT=stub|llm).
  - On chat message: build prompt with available tools (from registry), call
    LLM with function-calling; execute returned tool calls via the registry;
    stream/return the agent's natural-language reply to chat (broadcast over WS).
  - On card created: LLM optionally summarizes/auto-assigns (replaces stub note).
  - Use an OpenAI-compatible client (litellm or openai SDK) so any model works.
  - Keep API key in env (CREWSPACE_LLM_API_KEY, CREWSPACE_LLM_BASE_URL, CREWSPACE_LLM_MODEL).
    Stub remains default so the app runs with zero keys.

Implemented notes (2026-08-15):
  - LLMAgent lives in infrastructure/agents/llm.py; OpenAI SDK v3 (`AsyncOpenAI`)
    with `tool_choice="auto"`. Tool definitions are sourced verbatim from the
    Tool Registry (now full JSON Schema in application/tools.py). Multi-round
    tool-calling loop (max_tool_rounds=5) feeds results back as `role:"tool"`
    messages; final assistant text is returned as the reply. Client is injectable
    via `client_factory` for tests (mocked, no network/key).
  - `build_agent()` in infrastructure/agents/__init__.py selects stub vs llm and
    builds the registry for the agent's tool surface.
  - Acceptance: covered by tests/test_llm_agent.py (mocked tool-call execution,
    reply broadcast, function-definition shape, mention gating) + a full-stack
    integration test exercising ChatService -> LLMAgent -> real create_card.
    The `/tools` debug endpoint (M0 optional) is also added: `GET /tools`.

Files: `agent.py` (add LLMAgent) -> infrastructure/agents/llm.py; config additions
(sb already had CREWSPACE_LLM_* / CREWSPACE_AGENT); deps tweak; tests. Deps on M0 (done).

------------------------------------------------------------------------------
M2 — MCP Exposure                                              [M]  DONE
------------------------------------------------------------------------------
Scope:
  - Wrap the Tool Registry as an MCP server (official `mcp` SDK / FastMCP).
  - Expose: tools = registry tools; resources = board snapshot, channel history.
  - Run MCP alongside HTTP (separate ASGI app or `mcp.run(transport="sse")`),
    or as a standalone `uv run crewspace-mcp` entrypoint.
  - This lets an external LLM agent (Claude Desktop, another agent) discover and
    call YOUR board/chat as if it were one of its own tools.

Implemented notes (2026-08-15):
  - infrastructure/mcp_server.py wraps the SAME `build_registry()` as M0/M1 --
    no duplicated tool definitions. Uses mcp v2 `MCPServer` (FastMCP of v2).
  - Each registry tool becomes an MCP tool. Because the MCP framework derives a
    tool's *call* schema from the handler signature, handlers are generated
    dynamically with explicit named params (from each tool's JSON Schema) via
    exec, then the richer registry schema is re-attached as the advertised
    `parameters`. The handler body routes through `registry.bind(uow)` -- the
    agent's exact path -- committing on success, rolling back on error.
  - Resources: `board://{board_id}` (columns+cards) and
    `channel://{channel_id}` (messages), both JSON.
  - The server is a standalone process: it opens its own `Database` handle (the
    same `Database.create` seam) once per lifetime via the MCP `lifespan`, so
    there is a single storage seam with the rest of the app.
  - Console script `crewspace-mcp` (stdio/sse/streamable-http). Works with
    any MCP client. Configure DB via the same CREWSPACE_DB_PATH env var.

Acceptance (met):
  - An MCP client (mcp.Client over the in-process transport) lists the 6 tools
    and can call `create_card`/`find_card`, seeing effects in the same DB the
    web app uses. `board://` resource resolves. Covered by tests/test_mcp_server.py.

Files: new infrastructure/mcp_server.py; console script in pyproject
(`crewspace-mcp`); tests. Deps on M0+M1 (reuses the canonical tools).

------------------------------------------------------------------------------
M3 — Multi-channel / Multi-tenant                             [L]
------------------------------------------------------------------------------
Scope (schema changes in db.py):
  - Multiple workspaces (seeded one exists).
  - Multiple channels per workspace; channel kinds: 'channel' | 'dm'.
  - Channel membership table (already seeded for #general) → real membership.
  - Message threads: `parent_message_id` on message; UI + WS support replies.
  - Agent routing: `@planner` mention → that agent responds; DMs to an agent.
  - Pages: workspace/channel switcher; per-channel boards optional.
  - Auth-lite: a `current_user` concept (already KB current_user_id) → real
    member sessions later.

Acceptance:
  - Create 2 channels; post in each; WS isolates per channel.
  - Thread a reply under a message; it shows nested.
  - `@planner` in #general triggers the agent only there.
  - Existing tests updated; new tests for channel isolation + threads.

Files: db.py schema+migrations, ws.py (per-channel manager already keyed),
board.py/main.py pages, agent routing, tests. Independent of M1/M2 but touches
db.py (so do M4 — sequence M3 before M4).

------------------------------------------------------------------------------
M4 — Postgres persistence                                      [M–L]
------------------------------------------------------------------------------
Scope:
  - Replace aiosqlite with async Postgres (asyncpg + SQLAlchemy 2.0 / SQLModel,
    or raw asyncpg). Keep `db.py` as the ONLY seam that changes.
  - Connection pooling; env: CREWSPACE_DB_URL=postgresql+async://...
  - Schema migration: Alembic (or a simple `CREATE TABLE IF NOT EXISTS` init
    like today, upgraded for M3 columns). Seed script idempotent.
  - aiosqlite remains a dev fallback (CREWSPACE_DB_URL unset → sqlite) if desired.

Acceptance:
  - `CREWSPACE_DB_URL=postgres... uv run uvicorn ...` runs the full app; all M0–M3
    tests pass against Postgres (run suite against PG in CI or locally).
  - No route/agent/MCP code references sqlite specifics.

Files: db.py rewrite (engine + sessions), config, seed, tests. Deps on M3 schema.

------------------------------------------------------------------------------
M5 — Polish & Hardening (optional, ongoing)                   [S–M]
------------------------------------------------------------------------------
  - Multi-worker WS: Redis pub/sub for ConnectionManager.
  - Streaming agent replies (SSE/WS token stream) instead of one blob.
  - Auth: real login, workspace membership, agent permissions.
  - Observability: log tool calls, agent decisions; replay.
  - React SPA replacing HTMX (keep HTMX as the lightweight default).

------------------------------------------------------------------------------
M6 — Remote Engineering Agent Control Plane                   [XL]  PLANNED
------------------------------------------------------------------------------
Goal: evolve connected coding agents from chat responders into a governed,
inspectable, reliable software-delivery system. This is one umbrella milestone;
each numbered slice below is independently implementable, verifiable, and
committable.

Tracking rules:
  - Status values: PLANNED -> IN PROGRESS -> BLOCKED | DONE.
  - Update the tracker row, that slice's checklist, `Last updated`, and Progress
    log in the SAME verified milestone commit that changes implementation status.
  - `Progress` is objective acceptance completion (`checked / total`), not an
    estimated percentage. A slice is DONE only when every acceptance item is
    checked and its evidence field names tests plus commit(s).
  - Only one slice should normally be IN PROGRESS. Record blockers and dependency
    changes in the Notes column rather than carrying them only in chat.
  - `PROGRESS.md` points to the active slice; this file remains the durable
    cross-session source of truth for the whole milestone.

Last updated: 2026-08-24 (WIB)

| Slice | Deliverable | Status | Progress | Depends on | Evidence | Notes |
|------:|-------------|--------|----------|------------|----------|-------|
| M6.1 | Agent capability negotiation | DONE | 6/6 | current signed WS protocol | `e7aba78`; 107 focused tests + full sequential suite + hardened live POC + independent review | No blockers |
| M6.2 | Isolated worktrees and structured change sets | DONE | 7/7 | M6.1 | `6a78496`; 179-test bounded lifecycle/POC/security gate; migration round trip; independent fail-closed review | Same-process remote lifecycle idempotence; cross-restart reconstruction belongs to M6.3 |
| M6.3 | Durable and cancellable agent runs | IN PROGRESS | 4/8 | M6.1 | `4b199be` (item 1); `38c2e27` (item 2); item 3 persists bounded recent output + GET /api/coding/runs/{id}: `dispatch_coding_run` re-checks team↔repo grant via contracted `is_team_granted`, transitions queued→running in-UoW, dispatches distinct request_id; authenticated `POST /api/coding/runs`; 146-test bounded gate + independent fail-closed re-review (BLOCKERS: none) | Lifecycle + timestamps + fail-closed CAS complete; cancellation/restart/UI deferred to later items |
| M6.4 | Typed execution events and unified event envelope | PLANNED | 0/7 | M6.1, M6.3 | — | Includes replay cursor, ordering, and dedupe |
| M6.5 | Approval checkpoints and run-scoped policy | PLANNED | 0/7 | M6.3, M6.4 | — | Reuse existing default-deny tool/MCP governance |
| M6.6 | Multi-agent delivery pipeline | PLANNED | 0/7 | M6.2–M6.5 | — | Planner -> coder -> reviewer -> test -> human approval |
| M6.7 | Agent evaluation and reliability scorecards | PLANNED | 0/7 | M6.3, M6.4 | — | Replayable benchmarks and version/model comparisons |
| M6.8 | Operational inbox | PLANNED | 0/7 | M6.3–M6.5 | — | Human-attention queue across agents, workflows, MCP |

M6.1 — Agent capability negotiation                     [S–M]  DONE
Scope:
  - Add a versioned signed `hello`/capabilities frame after agent authentication.
  - Declare progress, cancellation, tools, artifacts, patches, resume support,
    concurrency, heartbeat, protocol version, and agent implementation version.
  - Persist/track the active connection's advertised capabilities without making
    stale disconnected values look live.
  - Gate server controls and dispatch features by negotiated capability.
  - Show useful UI state such as protocol version, capabilities, and busy slots.

M6.1 — Agent capability negotiation                     [S–M]  DONE
Acceptance (6/6):
  - [x] Versioned capability schema and compatibility rules are documented.
  - [x] Signed `hello` frame is identity-verified and rejects invalid values.
  - [x] Older agents without negotiation retain a safe, explicit legacy profile.
  - [x] Dispatch and UI gate features/capacity from the negotiated profile.
  - [x] Busy/legacy state updates live and is mutex-safe against reconnect races.
  - [x] Verified slice committed and pushed; commit evidence is recorded in the
        M6 progress log.

M6.2 — Isolated worktrees and structured change sets         [L]  DONE
Scope:
  - Allocate one isolated git worktree/branch per coding run.
  - Prevent two agents from mutating the same checkout.
  - Capture commits, changed files, diff summary, test/lint results, and artifacts.
  - Render a compact change-set card with review, PR, and discard actions.
  - Clean up worktrees safely after merge, cancellation, or explicit retention.

Acceptance (7/7):
  - [x] Every coding run receives a unique validated worktree and branch on its remote execution host.
  - [x] Execution-host repository/path authorization prevents traversal and cross-project writes.
  - [x] Concurrent remote runs cannot share a mutable checkout.
  - [x] Change-set DTO/UI shows files, commits, verification, and artifacts.
  - [x] Review/open-PR/discard are dedicated governed actions with audit events.
  - [x] Cleanup is idempotent and never deletes an unmerged retained workspace.
  - [x] Integration POC produces, verifies, reviews, and cleans a real change set.

M6.3 — Durable and cancellable agent runs                    [L]  IN PROGRESS
Scope:
  - Add persistent agent-run lifecycle: queued, running, succeeded, failed,
    cancelled, timed_out, and interrupted.
  - Correlate run id through chat request, progress, final reply, and audit data.
  - Add cancellation with signed agent acknowledgement and subprocess termination.
  - Restore active/recent run state after refresh; reconcile reconnect and app restart.
  - Handle late/duplicate frames and cancellation-vs-completion races idempotently.

Acceptance (3/8):
  - [x] Migration/model/repository expose the complete lifecycle and timestamps.
  - [x] Run creation and state transitions are transactional and authorization-scoped.
  - [x] Refresh restores status plus bounded recent output.
  - [ ] Cancellation terminates the example subprocess and reaches terminal state.
  - [ ] Disconnect/reconnect and app restart reconcile interrupted runs honestly.
  - [ ] Late, duplicate, and cancellation-race frames cannot duplicate final messages.
  - [ ] Dedicated run detail shows timeline, duration, result, and failure reason.
  - [ ] Unit/integration/live restart-and-cancel POCs pass.

M6.4 — Typed execution events and unified event envelope     [L]  PLANNED
Scope:
  - Define an envelope with event_id, event_type, occurred_at, actor_id,
    channel_id, run_id, correlation_id, sequence, and typed payload.
  - Cover plan, file, command, test, artifact, approval, warning, and terminal events.
  - Unify agent progress, workflow progress, presence, and tool audit delivery where
    semantics align; preserve explicit bounded adapters for legacy frames.
  - Support ordered replay/resume cursors and deduplication across reconnects.

Acceptance (0/7):
  - [ ] Versioned schemas exist for envelope and initial typed event catalog.
  - [ ] Per-run sequence/order and event-id dedupe are deterministic.
  - [ ] Reconnect resumes from a cursor without gaps or duplicate UI entries.
  - [ ] UI renders compact typed activity with raw logs available on demand.
  - [ ] Audit JSON/CSV exports include the same canonical events.
  - [ ] Transport seam supports a future Redis/multi-worker implementation.
  - [ ] Contract, replay, reconnect, and migration-compatibility tests pass.

M6.5 — Approval checkpoints and run-scoped policy            [M–L]  PLANNED
Scope:
  - Add explicit approval requests for writes, commands, network, package install,
    git push/PR, deployment, and other consequential operations.
  - Support one-time, run-scoped, and policy-derived decisions with expiry.
  - Enforce at discovery/advertisement and execution, reusing current agent-tool and
    external MCP default-deny policy rather than adding a parallel authorization path.

Acceptance (0/7):
  - [ ] Consequential action classes and default-deny policy are documented.
  - [ ] Approval request/decision is persisted and tied to principal, run, and action.
  - [ ] Dedicated app-shell approval form shows exact operation and consequences.
  - [ ] Denied/expired/replayed approvals cannot execute.
  - [ ] Policy is enforced at both capability discovery and execution.
  - [ ] Every request, decision, and attempted use is auditable.
  - [ ] Security tests cover impersonation, scope escalation, replay, and races.

M6.6 — Multi-agent delivery pipeline                         [L–XL]  PLANNED
Scope:
  - Orchestrate explicit planner -> coder -> reviewer -> tester -> human approval stages.
  - Pass structured handoff artifacts, not unconstrained agent-to-agent chat.
  - Apply stage budgets, timeouts, terminal states, and no-free-loop safeguards.
  - Display one run graph with stage ownership and independent reviewer context.

Acceptance (0/7):
  - [ ] Versioned handoff contracts define required inputs/outputs per stage.
  - [ ] Pipeline graph has deterministic transitions and bounded retry policy.
  - [ ] Reviewer receives independent context plus immutable change-set evidence.
  - [ ] Failed/cancelled stages cannot silently advance or duplicate downstream work.
  - [ ] Human approval is required before configured delivery actions.
  - [ ] UI shows stage status, owner, artifacts, budgets, and blockers.
  - [ ] End-to-end real-repo POC reaches a verified human-approved change set.

M6.7 — Agent evaluation and reliability scorecards           [M–L]  PLANNED
Scope:
  - Track success, timeout/disconnect, latency, tool failures, cancellation response,
    verification delta, human acceptance/rework, token usage, and cost where available.
  - Build replayable benchmark tasks and compare agent/model/version cohorts.
  - Separate product success metrics from transport health and model quality.

Acceptance (0/7):
  - [ ] Metric definitions, denominators, and privacy/retention policy are documented.
  - [ ] Run/event data produces deterministic aggregate metrics.
  - [ ] Benchmark fixtures are replayable and isolated from production workspaces.
  - [ ] Scorecards compare agent implementation/model versions without misleading mixes.
  - [ ] Regression thresholds can block rollout without auto-promoting a winner.
  - [ ] UI links every aggregate to inspectable supporting runs.
  - [ ] Seeded benchmark POC demonstrates a version comparison and regression alert.

M6.8 — Operational inbox                                     [M–L]  PLANNED
Scope:
  - Create a unified human-attention queue for approval requests, failed/timed-out
    runs, disconnected agents with active work, workflow failures, pending MCP
    approvals, requested reviews, and stale tasks.
  - Provide filters, ownership, priority, acknowledgement, resolution, and deep links.
  - Keep this as a projection over source records/events, not a second source of truth.

Acceptance (0/7):
  - [ ] Inbox item taxonomy and source-to-item projection rules are documented.
  - [ ] Items dedupe deterministically and update/resolve with their source record.
  - [ ] Authorization prevents cross-tenant information leakage.
  - [ ] Dedicated app-shell inbox supports filter, assign, acknowledge, and resolve.
  - [ ] Every item deep-links to the relevant run/workflow/tool/review detail.
  - [ ] Live updates and reconnect replay preserve correct unread counts.
  - [ ] Integration POC exercises at least one item from each supported source.

M6 Progress log (append-only, newest first):
  - 2026-08-24 — M6.2 durable change-set governance GREEN: logical repositories
    are authorized many-to-many per team; coding runs bind team, repository,
    requester, remote agent, request, and instruction. Authenticated signed ingress
    validates the active request/repository/run correlation, then atomically stores
    the path-free change set, capture audit, and run status before completing the
    remote waiter. Team-scoped list/detail pages render commits, files, verification,
    artifact metadata, and audit history. Review, request-PR, retain, and
    request-discard each use a dedicated app-shell workflow with compare-and-set
    transitions and authenticated actor audit. A 152-test bounded management,
    protocol, real-Git, and security gate passes; schema drift, compilation, and
    diff checks pass; fresh migration upgrade/downgrade/upgrade and live HTTP UI
    proofs pass. Browser-DOM proof was blocked by Chrome's local remote-debugging
    approval prompt. Final independent verdict: no blockers. Physical remote PR and
    cleanup execution, run-start UI, and the full real-repository POC remain pending.
  - 2026-08-24 — M6.2 structured capture GREEN: immutable DTOs capture ordered
    commits, file status and line totals, verification records, and metadata-only
    workspace artifacts. Allocation provenance, branch/workspace identity, clean
    tracked state, declared untracked files, traversal, and external symlinks are
    enforced. Frozen provenance, ignored/odd filenames, consistent no-rename parsing,
    mutation fingerprints, deep-frozen collections, lossless NUL parsing, and
    bounded streaming subprocess output/time are enforced. The Git adapter now
    lives on the remote execution host; 21 real-Git tests pass within a 136-test
    bounded protocol/security gate. Final independent verdict: no blockers.
  - 2026-08-24 — M6.2 architecture corrected: Crewspace dispatches only opaque
    repository/run IDs over the signed agent socket; the remote execution host owns
    its operator-configured repository map, Git roots, worktree allocation, Claude
    execution, and capture. Git/common-directory identity is revalidated before
    allocation; nested roots and path replacement are rejected. Eight parallel
    real-Git allocations use distinct branches/checkouts. Twelve focused tests
    plus security regressions pass; independent final review found no blockers.
  - 2026-08-24 — M6.2 allocation tracer GREEN: remote execution-host repository
    allowlist, strict run-id validation, random branch/worktree allocation,
    bounded collision retry, and rollback after partial Git failures. Seven
    real-Git tests pass; independent re-review passed with no blockers.
  - 2026-08-24 — M6.2 moved to IN PROGRESS. First RED/GREEN slice defines the
    structured workspace contract and allocates unique validated branches and
    worktrees from an explicit remote execution-host repository allowlist.
  - 2026-08-24 — M6.1 DONE (6/6), verified implementation commit `e7aba78`.
    Independent final review found no blockers or
    suggestions after directly verifying reconnect/disconnect teardown and the
    explicit legacy activity boundary. Pushed to `origin/master` with the
    milestone documentation update.
  - 2026-08-24 — M6.1 implementation complete: versioned signed `hello`, explicit
    legacy profile, capability gating for progress/tools, busy-slot routing with
    server-reserved vs agent-reported separation, and live sidebar/management UI.
    Final gate: 107 focused tests pass; all remaining suite files pass in
    sequential groups with one key-gated skip; compile/diff/security scans pass.
    Hardened live POC proves legacy compatibility, v1 session sequencing, and
    exact replay rejection. Follow-up TDD fixes immediately fail/remove waits and
    reservations on reconnect/disconnect and reject v1-only activity from legacy
    agents. Final independent review passed with no blockers or suggestions.
  - 2026-08-24 — M6.1 moved to IN PROGRESS. Implementation order: versioned
    contract -> signed negotiation/legacy profile -> feature gates -> busy slots
    -> live UI -> example/POC. Each behavior is developed RED-GREEN.
  - 2026-08-24 — M6 created; all eight slices PLANNED. M6.1 selected as NEXT
    because capability negotiation establishes safe compatibility for cancellation,
    artifacts, structured events, and concurrency controls.

------------------------------------------------------------------------------
UI POLISH (2026-08-15)
------------------------------------------------------------------------------
The HTMX UI had four user-visible bugs, all fixed:
  - Chat avatar: rendered as a separate text node (was `who.textContent =
    (m.avatar||"")+name+":"`, which silently dropped the avatar on null). Now an
    avatar span + a name text node, so the 🤖 shows for agents and null is safe.
  - New card position: create re-renders the WHOLE column sorted by `position`,
    so a new card lands at the bottom (no longer appears "out of order").
  - Move between columns: re-renders the WHOLE board (`board_fragment.html`) and
    swaps `#board-wrap` innerHTML — the bulletproof HTMX pattern (no OOB
    ambiguity, so a moved card never lingers in its old column). The dropdown
    uses `hx-trigger="change"` on the `<select>`; drag-and-drop POSTs via
    `htmx.ajax(... target:'#board-wrap', swap:'innerHTML')`.
  - CRITICAL: HTMX is vendored locally (`src/crewspace/static/htmx.min.js`,
    served at `/static/htmx.min.js`). Do NOT load it from a CDN — corporate/
    offline networks block unpkg.com, which made `window.htmx` undefined and
    broke ALL interactivity (move, create, drag-drop) during UAT.
  - Drag-and-drop: implemented (cards are `draggable`; dropping on a column POSTs
    `/cards/{id}/move` via `htmx.ajax(... target:'#board-wrap', swap:'innerHTML')`,
    the same whole-board re-render as the dropdown).
  - Nav: moved from a top bar to a LEFT SIDEBAR (layout.html), with Chat/Board
    links and a member-avatar strip.

Files: templates/layout.html (new), board.html, card.html, column.html (new),
       chat.html, comment.html; api/routers/boards.py + cards.py;
       application/services.py (move_card returns old+new column ids).

------------------------------------------------------------------------------
Suggested order & dependencies
------------------------------------------------------------------------------
  M0 (tool registry)
    ├─► M1 (LLM agent)        ──┐
    ├─► M2 (MCP exposure)     ──┤── both consume M0 tools
    └─► M3 (multi-channel) ──► M4 (Postgres)   (schema changes sequenced)
  M5 anytime after M1/M3.
  M6.1 (capabilities) -> M6.2 (worktrees) + M6.3 (durable runs)
    -> M6.4 (typed events) -> M6.5 (approvals) -> M6.6 (pipeline)
    -> M6.7 (evaluation) + M6.8 (operational inbox).

Fastest path to "impressive demo": M0 → M1 → M2 (one agent that both thinks
and is callable by other agents). Add M3+M4 for production realism.

------------------------------------------------------------------------------
Risks / notes
------------------------------------------------------------------------------
  - LLM tool-calling format differs per provider; use litellm or openai SDK to
    normalize. Validate tool args against JSON Schema before execution.
  - MCP SDK maturity: pin versions; SSE transport is simplest to co-host.
  - Postgres migration is mechanical ONLY because db.py already isolates SQL;
    do not let routes import the driver directly (enforce in review).
  - Keep StubAgent as the default so the project always runs keyless.
