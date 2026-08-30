# Crewspace — Board as the Agent Operating Surface (M7)

Status: PLANNED
Dependencies: M6.1–M6.8 (agent capabilities, runs, workflows, approvals, inbox).

Goal: turn the board from a minimal Kanban surface into the visible operating
layer for the agent control plane. It must carry real task metadata, live
multi-user updates, and explicit linkage to coding runs, change sets, approvals,
and workflows — so the board is where work is created, routed, and reviewed, not
just where cards are moved.

Verified-slice discipline applies to every slice below: RED test first, bounded
test gate, migration-compat guard, compileall/diff/security checks, independent
fail-closed review, then one [verified] commit + push. Each slice records the
feature behavior and the concrete code touched (see the per-slice "Feature /
Code" blocks). No slice is marked DONE until its acceptance items are checked and
evidence (tests + commit) is recorded in the M7 progress log.

--------------------------------------------------------------------------------
M7.1 — Card detail view and edit                         [M]  PLANNED
--------------------------------------------------------------------------------
Feature:
  Clicking a card opens a dedicated detail surface (a modal, or a route
  /boards/{board_id}/cards/{card_id}). The detail view shows and lets the acting
  user edit: title, Markdown description, assignee, due date, priority, and
  labels. Comments and an edit-history trail are preserved. Required fields and
  authorization reuse the existing board-access checks.

Code (concrete):
  - domain/entities.py: extend Card/CardView with `due_date`, `priority`,
    `labels`, and an edit-audit list; keep composed read-model pattern.
  - application/services.py BoardService: add `get_card`, `update_card`,
    `set_assignee`, `set_due_date`, `set_priority`, `set_labels`,
    `add_card_activity`.
  - domain/ports.py + infrastructure/repositories.py: add the read/write methods
    on the board port + SqlAlchemy implementation (extend the port, don't add a
    parallel class).
  - infrastructure/models.py + a migration: new columns
    (`due_date`, `priority`, `labels`, `card_activity` table for history).
  - api/routers/boards.py + templates/: add `card_detail.html`,
    `card_edit.html` (or a modal fragment) and HTMX endpoints to save each field.
  - dto/: a CardDetailDTO (pure, sqlalchemy-free) for the detail + agent tools.

Acceptance:
  - [ ] Clicking a card opens a detail surface with title/description/assignee/due/priority/labels and comments.
  - [ ] Each field is editable through a dedicated HTMX action and persists an edit-history record.
  - [ ] Description renders server-side as Markdown.
  - [ ] Card detail is board-authorized and fails closed for unknown/unpermitted access.
  - [ ] The new Card fields are migration-compat guarded (pure DTO, makemigrations --check clean).
  - [ ] Agent tool surface gains `get_card` / `update_card` so agents read/update card metadata.
  - [ ] Feature + code documented in the M7 progress log with tests + commit evidence.

--------------------------------------------------------------------------------
M7.2 — Board & column management + board switcher         [M]  PLANNED
--------------------------------------------------------------------------------
Feature:
  Create, rename, archive/unarchive, and delete boards from the UI, scoped to the
  existing workspace/team hierarchy. A board switcher in the sidebar/topbar lists
  the user's accessible boards. Same for columns: add, rename, reorder, archive.

Code (concrete):
  - application/services.py BoardService: `create_board`, `rename_board`,
    `archive_board`, `list_accessible_boards` (reuse access.py),
    `create_column`, `rename_column`, `reorder_column`, `archive_column`.
  - application/access.py: reuse `list_accessible_boards` / `can_access_board`.
  - domain/ports.py + repositories.py: `boards.create`, `boards.rename`,
    `boards.archive`, `boards.list_for_member`; column CRUD on the port.
  - infrastructure/models.py + migration: add `archived_at` / `archived` columns
    (board, board_column).
  - api/routers/boards.py + templates/: board switcher into `layout.html`
    navigation_context, `board_settings.html`, column header menu.
  - dto/: BoardCommandDTO (pure) for create/rename actions; safe ids only.

Acceptance:
  - [ ] A user can create/rename/archive a board they can access; the board switcher reflects it.
  - [ ] Columns can be added, renamed, reordered, archived from the board UI.
  - [ ] Board list respects workspace/team authorization (no cross-workspace leakage).
  - [ ] Archived boards/columns are hidden from the default view but recoverable.
  - [ ] Migration-compat guard clean; makemigrations --check passes.
  - [ ] Feature + code documented in the M7 progress log with tests + commit evidence.

--------------------------------------------------------------------------------
M7.3 — Live board updates over WebSocket                  [M]  ACTIVE
--------------------------------------------------------------------------------
Feature:
  Board mutations (create/move/comment/edit) broadcast a minimal delta to all
  members viewing that board, so multiple people see changes live without a
  page reload. Replace the current "re-render the whole board for the acting
  client only" behavior on the read path with targeted per-board broadcasts;
  keep the whole-board swap for the acting client's own feedback.

Status: implementation RED→GREEN complete; bounded gate green; independent
fail-closed review BLOCKERS: none; committed + pushed as f0a81b7.

Code (concrete):
  - api/connection.py: a per-board broadcast room (reuse ConnectionManager with a
    room key like `board:{board_id}`).
  - api/routers/boards.py + cards.py: publish a `board_delta` frame (kind:
    card_created / card_moved / card_updated / comment_added, card_id, …) via
    manager.broadcast(board_room, ...) after each mutation.
  - templates/board.html + static JS: subscribe to the board room; apply deltas
    (insert/move/update card DOM) without full re-render for non-acting clients.
  - dto/: a BoardDeltaDTO (pure) describing the minimal mutation for the wire.
  - Bounded test: two TestClient WS connections on the same board; one mutates,
    the other receives the matching delta.

Acceptance:
  - [x] A card created/moved/commented/edited by one client reaches other viewers of that board live.
  - [x] Deltas carry enough data to update the card in place (no board reload needed).
  - [x] Broadcast is board-scoped; members of other boards/workspaces receive nothing.
  - [x] Acting client still gets its canonical whole-board feedback (no regression).
  - [x] Feature + code documented in the M7 progress log with tests + commit evidence.

--------------------------------------------------------------------------------
M7.4 — Card ↔ coding-run / change-set linkage             [L]  VERIFIED
--------------------------------------------------------------------------------
Feature:
  A card can be linked to coding runs and resulting change sets. The card shows
  run status and change-set/review/approval state as badges, and deep-links to
  the run, change set, review, and inbox item. Creating a card can optionally
  spawn a coding run (e.g. `@crewspace implement card X`), and run outcomes
  retroactively annotate the card.

Code (concrete):
  - domain/entities.py + models.py + migration: `card_run_link` (card_id,
    run_id, linked_by, linked_at) and a `linked_change_set` reference.
  - application/run_policy.py / coding_runs.py: when a run completes, update the
    linked card status (succeeded/failed/timed_out/cancelled) idempotently.
  - application/services.py BoardService: `link_card_to_run`, `card_run_status`.
  - api/routers/boards.py + templates/card.html: status badges + deep links to
    `/api/coding/runs/{id}` and `/management/change-sets/{id}` (reuse href
    conventions from scorecard/inbox); a "spawn coding run" action per card.
  - application/tools.py: a board tool to dispatch a run from a card.
  - dto/: CardRunStatusDTO (pure) for the wire/badges.

  Fail-closed invariants (assert in tests):
  - A card-to-run link is created only from authenticated state (team, requested_by), never from agent/remote input.
  - Run outcomes update the card only for a genuinely linked, still-live card (idempotent, no cross-team leak).
  - Unauthorized link reads/reveal nothing.

Acceptance:
  - [x] A card can produce a coding run and display its live status badge.
  - [x] Completed runs annotate the card with change-set/review/approval state and deep links.
  - [x] Link creation and outcome updates are fail-closed and team-authorized.
  - [x] Migration-compat guard clean; makemigrations --check passes.
  - [x] Feature + code documented in the M7 progress log with tests + commit evidence.

--------------------------------------------------------------------------------
M7.5 — Move-to-column workflow triggers                   [L]  PLANNED
--------------------------------------------------------------------------------
Feature:
  Moving a card to a configured column starts a workflow or pipeline stage
  (planner → coder → reviewer → human approval). Column→workflow mappings are
  configurable per board; a move to a trigger column enqueues the run and the
  card reflects it. This turns the board into a visual workflow orchestrator
  reusing the existing workflow executor and M6.6 delivery pipeline.

Code (concrete):
  - application/workflows.py: a `column_move` trigger source (like the existing
    message/webhook/schedule triggers) that dispatches on card move.
  - application/services.py BoardService.move_card: after a valid move, evaluate
    the board's column→workflow mapping and enqueue matching runs (fail-closed,
    within the same authorized UoW context).
  - Infrastructure: store column→workflow mappings (new table or config column)
    with a migration; guard against infinite loops and duplicate triggers.
  - api/routers/boards.py + templates: a board settings UI to configure
    column→workflow rules (deep link to the workflow detail).
  - dto/: ColumnTriggerDTO (pure) for the config; MVCC/cas where needed to avoid
    double-trigger on retries.

  Fail-closed invariants (assert in tests):
  - A move only triggers a run when the actor is authorized and the rule exists.
  - Duplicate/retried moves cannot double-enqueue the same workflow (idempotent trigger key).
  - A misconfigured/nonexistent workflow never silently advances a card.

Acceptance:
  - [ ] Moving a card into a trigger column enqueues the configured workflow/pipeline.
  - [ ] Column→workflow mapping is configurable per board via the UI.
  - [ ] Triggers are idempotent (no double enqueue) and fail closed on misconfiguration.
  - [ ] Card reflects the resulting run/pipeline state.
  - [ ] Migration-compat guard clean; makemigrations --check passes.
  - [ ] Feature + code documented in the M7 progress log with tests + commit evidence.

--------------------------------------------------------------------------------
M7.6 — Board planning views: filters, group-by, timeline    [L]  PLANNED
--------------------------------------------------------------------------------
Feature:
  Board-level planning surfaces: filter/group cards by assignee, agent, label,
  priority, due, or status; swimlanes by assignee/agent; and a timeline view
  driven by due dates. Saved views per user. Reuses the scorecard/metrics data
  for light cycle-time/throughput aggregates.

Code (concrete):
  - application/board_views.py (pure view model): build filters, group-by, and
    timeline projections from CardView data; reuse `compute_*` patterns from
    scorecard_view where appropriate.
  - api/routers/boards.py: query params for filter/group/view; a timeline
    fragment route.
  - templates/: `board_views.html`, `swimlane.html`, `timeline.html` fragments.
  - dto/: BoardFilterDTO / BoardGroupDTO (pure, sqlalchemy-free).
  - Tests: pure view-model tests (no app fixture) + a routed fragment test.

Acceptance:
  - [ ] Cards can be filtered/groupped by assignee, agent, label, priority, due, status.
  - [ ] Swimlane-by-assignee/agent view renders correctly.
  - [ ] Timeline view groups cards by due date and marks overdue.
  - [ ] Saved per-user views persist; authorization scoped.
  - [ ] Feature + code documented in the M7 progress log with tests + commit evidence.

--------------------------------------------------------------------------------
M7.7 — Board operating-surface integration POC             [M]  PLANNED
--------------------------------------------------------------------------------
Feature:
  An end-to-end seeded POC (no production data) that exercises the whole
  milestone: create a board, add a card with metadata, link it to a coding run,
  move it into a trigger column to start a workflow, comment, and verify live
  deltas reach a second WS viewer and the inbox shows any resulting attention
  items. Seeded, deterministic, isolated — mirrors the M6.8 inbox POC pattern.

Code (concrete):
  - application/board_poc.py: `run_board_poc()` building a seeded fixture and
    walking the acceptance flows, asserting each step (like benchmark_poc /
    inbox_poc).
  - tests/test_board_poc.py: the gated POC test.
  - Ties together M7.1–M7.6 surfaces.

Acceptance:
  - [ ] POC creates boards/cards, edits metadata, links a run, triggers a
        workflow on move, comments, and observes live deltas + inbox items.
  - [ ] POC is deterministic and isolated (no production DB/workspace).
  - [ ] Feature + code documented in the M7 progress log with tests + commit evidence.

--------------------------------------------------------------------------------
M7 Progress log (append-only, newest first):
--------------------------------------------------------------------------------
- M7.4 — Card ↔ coding-run / change-set linkage — verified; BLOCKERS: none.

  Feature: cards can be durably linked to one or more coding runs. The board
  renders each linked run's live lifecycle state as a badge and, once capture
  exists, adds change-set and review badges with canonical deep links. The
  `spawn_coding_run_from_card` board tool derives team/requested-by identity
  from authenticated state, dispatches the run, and links it to the source
  card. Run outcomes need no duplicate annotation write: the card projection
  joins the linked run and its immutable change set live, so retries remain
  idempotent and terminal status changes appear on the next render.

  Code:
  - domain/entities.py + infrastructure/models.py: `CardRunLink`,
    `CardRunStatusView`, and `card_run_link` with composite identity
    `(card_id, run_id)` and card/run/member foreign keys.
  - migration `20260830_01_card_run_links.py`: idempotent link-table creation
    above `20260826_02`, with downgrade support.
  - domain/ports.py + infrastructure/repositories.py: idempotent
    `link_card_run` (`ON CONFLICT DO NOTHING`) plus card- and board-scoped live
    projections joining `coding_run` and `stored_change_set`.
  - dto/board.py + dto/mappers.py: strict, frozen, SQLAlchemy-free
    `CardRunStatusDTO`, mapper, and canonical run/change-set/review badge links.
  - application/services.py: `link_card_to_run`, `card_run_status`, and batch
    `board_run_statuses`; board access, run-team equality, and team-management
    authorization are rechecked before linking; unauthorized reads return no
    link data.
  - application/tools.py: authenticated `spawn_coding_run_from_card` tool;
    derives the team from card → board → workspace and rechecks board scope,
    team management, and repository grant before dispatch/link.
  - api/routers/boards.py + templates/card.html: per-card live status lookup and
    compact run/change-set/review deep-link badges.
  - tests/test_board_run_links.py: 9 focused tests covering the real service,
    repository, migration, change-set capture, badge, and registry-handler
    paths; only remote agent transport is stubbed in the spawn-tool test.

  Evidence: focused M7.4 tests green (9 passed); bounded board + live +
  change-set regression gate green (108 passed); fresh-head migration drift
  check clean at `20260830_01`; legacy `20260826_02` downgrade/upgrade
  round-trip green; compileall OK; git diff --check clean; added-line security
  scan clean. Independent fail-closed review: BLOCKERS: none, NON-BLOCKERS:
  none. Committed as 6a10d78.

- M7.3 — Live board updates over WebSocket — verified; BLOCKERS: none.
  Agent-originated mutations broadcast via the registry publisher seam
  (api/board_live.py); standalone MCP is a separate process (no web
  ConnectionManager) and is documented as out of the live-room scope.

  Feature: each accessible board has an authorized WebSocket room
  (`board:{board_id}`). Card create/move/edit/comment mutations publish typed
  `board_delta` frames into that room. Non-acting viewers apply the canonical
  server-rendered card/comment fragments directly to the affected DOM node,
  while the acting client keeps the existing HTMX whole-board response.
  Card moves physically relocate the fragment between columns; card/comment
  self-echoes and reconnect replays are idempotently ignored.

  Code:
  - dto/board.py: pure `BoardDeltaDTO` wire contract with constrained kinds,
    safe card/column/comment ids, and optional canonical HTML fragments.
  - api/routers/boards.py: authorized `/boards/{board_id}/ws` subscription to
    `board:{board_id}`; create/update broadcasts.
  - api/routers/cards.py: move/comment broadcasts, including stable comment id.
  - templates/board.html + static/board_live.js: board-room subscriber and
    targeted card create/move/update/comment DOM applier; self-echo/replay
    dedupe; unknown kinds no-op; reconnect loop.
  - templates/comment.html: stable `comment-{id}` DOM identity for replay-safe
    comment deltas.
  - api/board_live.py: `board_room` + `build_board_delta_publisher` adapter;
    renders the canonical `card.html`/`comment.html` fragments (via
    uow.boards.get_board + dto.mappers.to_board) and broadcasts to the room.
  - application/tools.py: `BoardDeltaPublisher` seam; `_publish` hooks in
    create_card/move_card/comment_card/update_card so agent-originated
    mutations broadcast through the web composition roots (registry dep,
    agent WS); standalone MCP keeps the no-op default.
  - tests/test_board_live.py: BoardDeltaDTO, board-room isolation, authorized
    WebSocket endpoint coverage (4003 rejection), real-handler agent-tool
    publisher tests, and a real-adapter render/broadcast test.
  - tests/test_board_live_js.py: Node DOM shim executes the REAL client script
    and verifies create/move/update/comment in-place updates, physical
    cross-column relocation, self-echo/replay dedupe, and unknown-kind no-op.

  Evidence: bounded gate green (board + live + security + tool + MCP groups,
  88 passed); `makemigrations --check` clean at head 20260826_02 (no schema
  change); compileall OK; git diff --check clean; added-line security scan
  clean. Independent fail-closed review: BLOCKERS: none, NON-BLOCKERS: none
  (one doc nit fixed). Committed + pushed as f0a81b7.

- M7.2 — Board & column management + board switcher — [verified] committed.

  Feature: create/rename/archive/restore boards from the UI (dedicated
  app-shell forms), a board switcher in the sidebar listing only boards the
  user can access (no cross-workspace leakage), board settings surface that
  adds/renames/reorders/archives/restores columns (archived columns listed with
  RESTORE actions), column-header management
  menus, and archived board/columns hidden from the default view but fully
  recoverable via the recovery index and switcher. Reorder targets must be
  active siblings of the same board (missing/archived/foreign rejected);
  archived columns reject card creation and moves (HTTP + agent tools).

  Code:
  - domain/entities.py: Board/Column gained `archived_at`; BoardView/ColumnView
    carry it.
  - dto/board.py: BoardDTO += workspace_id; ColumnDTO/BoardDTO carry
    archived_at; BoardCommandDTO (pure create/rename DTO).
  - domain/ports.py BoardRepository: create/rename/archive/restore,
    list_columns_active, plus column CRUD (rename/reorder/archive/restore) and
    list_columns.
  - infrastructure/models.py: BoardModel/BoardColumnModel += archived_at.
    repositories.py: get_board/list_all/list_for_member hydrate archived_at;
    board + column CRUD; active/archived column queries; fail-closed archived
    target guards.
  - migration 20260826_02_board_column_archiving.py: idempotent ADD COLUMN
    archived_at (board, board_column).
  - application/services.py BoardService: create_board/rename_board/
    archive_board/restore_board/create_column/rename_column/reorder_column/
    archive_column/restore_column/list_columns_active; empty-name guards.
    application/access.py: can_access_workspace, can_manage_archived_board
    (recovery gate), list_accessible_boards.
  - api/routers/boards.py: POST /boards, POST .../rename|archive|restore,
    columns CRUD; GET /boards/new + GET /boards/{id}/settings (static routes
    registered BEFORE dynamic GET /{board_id} so "new"/"settings" aren't eaten
    as board ids); GET /board recovery index.
  - api/routers/pages.py + rendering.py: board page redirect-to-index for
    archived; sidebar `boards_menu` switcher (live + archived + team name).
  - templates/: board_index.html, board_new.html, board_settings.html,
    layout.html sidebar switcher, column.html header actions menu + card.html
    dynamic move dropdown (no more hardcoded col_todo/col_doing/col_done).

  Evidence: tests/test_board_management.py (19 tests) — red first for missing
  board/column management, then green; reviewer-driven RED tests additionally
  covered archived-column recovery UI, invalid/archived/foreign reorder targets,
  and rejecting card creation/moves into archived columns before returning green;
  bounded gate (test_board_card_detail, test_board_management, test_management,
  test_security, test_app, test_agent_tool_policy, test_mcp_server) green
  (excluding pre-existing test_lifespan_closes_an_injected_database_once, which
  fails on master too). Migration drift clean (head 20260826_02) incl. legacy
  upgrade + downgrade round-trip; compileall OK; git diff --check clean;
  added-line security scan clean; independent fail-closed reviews: initial
  findings (archived-column recovery UI + fail-closed reorder target) were
  remediated with RED regression tests; final independent re-review:
  BLOCKERS: none, NON-BLOCKERS: none.
  Commit: 40ea7be (feat(board): M7.2 — board and column management [verified]).
- M7.1 — Card detail view and edit — [verified] committed + pushed.

  Feature: clicking a card opens a detail view editing title, Markdown
  description, assignee, due date, priority, and labels; server-side Markdown
  preview; edit history via a `card_activity` audit trail; live badges on the
  board; priority/assignee validation; fail-closed board authorization; and
  policy-enforced agent `get_card`/`update_card` tools. Empty optional fields
  can be cleared from the UI; an empty title is rejected at the service.

  Code:
  - domain/entities.py: CardView += due_date/priority/labels/activity; new
    CardActivityView.
  - dto/board.py: CardDTO += fields; new CardDetailDTO. dto/mappers.py:
    to_card carries fields; to_card_detail.
  - domain/ports.py BoardRepository: update_card, set_assignee,
    list_card_activity.
  - infrastructure/models.py: CardModel += due_date/priority/labels; new
    CardActivityModel. repositories.py: get_card/_column_with_cards hydrate new
    fields; update_card (empty-string clears, activity per change); set_assignee
    (no-noise on no-op); list_card_activity/_add_activity; _parse_labels/
    _json_labels helpers.
  - migration 20260826_01_card_detail_metadata.py: idempotent ADD COLUMN +
    card_activity table + backfill of get_card/update_card for legacy builtin
    agents.
  - application/services.py BoardService: get_card_detail, update_card (empty
    title guard), set_assignee. application/tools.py: get_card (read) and
    update_card (write) native tools, board-scoped.
  - api/routers/boards.py: GET/POST /boards/{board_id}/cards/{card_id}
    (+ require_board_access + _require_card_in_board); card.html badges + title
    link; templates/card_detail.html.

  Evidence: tests/test_board_card_detail.py (10 tests), test_agent_tool_policy.py
  (+2 tool tests), test_security.py (+1 authz test) — all green.
  Migration drift clean (head 20260826_01); compileall OK; git diff --check
  clean; added-line security scan clean; independent review BLOCKERS: none.
  Commit: 33f9874 (feat(board): M7.1 — card detail view and edit [verified]).
