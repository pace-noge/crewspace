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
M7.3 — Live board updates over WebSocket                  [M]  PLANNED
--------------------------------------------------------------------------------
Feature:
  Board mutations (create/move/comment/edit) broadcast a minimal delta to all
  members viewing that board, so multiple people see changes live without a
  page reload. Replace the current "re-render the whole board for the acting
  client only" behavior on the read path with targeted per-board broadcasts;
  keep the whole-board swap for the acting client's own feedback.

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
  - [ ] A card created/moved/commented/edited by one client reaches other viewers of that board live.
  - [ ] Deltas carry enough data to update the card in place (no board reload needed).
  - [ ] Broadcast is board-scoped; members of other boards/workspaces receive nothing.
  - [ ] Acting client still gets its canonical whole-board feedback (no regression).
  - [ ] Feature + code documented in the M7 progress log with tests + commit evidence.

--------------------------------------------------------------------------------
M7.4 — Card ↔ coding-run / change-set linkage             [L]  PLANNED
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
  - [ ] A card can produce a coding run and display its live status badge.
  - [ ] Completed runs annotate the card with change-set/review/approval state and deep links.
  - [ ] Link creation and outcome updates are fail-closed and team-authorized.
  - [ ] Migration-compat guard clean; makemigrations --check passes.
  - [ ] Feature + code documented in the M7 progress log with tests + commit evidence.

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
  (empty — M7 is PLANNED; each verified slice appends its record here.)
