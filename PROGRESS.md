# Crewspace — Session Progress (fresh-session handoff)

Last updated: 2026-08-31 (WIB)
Repository: `/home/bilal/Projects/Learning/python/crewspace`
Branch: `master` → `origin/master`
Latest verified product commit before this handoff: `c7309e8` (M7.7)
Handoff state: this PROGRESS.md commit and `origin/master` are synchronized.
Worktree at handoff: clean

## Current milestone state

- M6.1 through M6.8 are complete.
- M6.7 is DONE 7/7 and tagged `milestone-m6.7` at `a2bec64`.
- M6.8 — Operational inbox — is DONE 7/7.
- M6.8 implementation/release tag: `milestone-m6.8` → `6b734bc`
  (`6b734bcd8af58c54ebc7434fbb18dcd8872a7a61`).
- Latest verified M7 product slice is M7.7 at the commit recorded below.
- M7 — Board as the Agent Operating Surface — is COMPLETE (7/7 slices).
  `PLAN.md` has the tracker; `PLAN_M7_BOARD.md` is the canonical detailed plan
  and carries the append-only per-slice progress log. Every slice documents its
  user-visible Feature and concrete Code touchpoints, then follows the verified
  RED→GREEN/review/commit/push workflow.

## M7.7 — Board operating-surface integration POC — verified

Feature: `application/board_poc.py` `run_board_poc()` walks the whole M7 stack
end to end on an isolated seeded DB: creates a POC board in ws_default and a
card with editable metadata (priority/labels/assignee), grants a POC repo and
dispatches a coding run to agent_planner (stubbed transport) then links the card
to it, creates a column_move workflow in chan_general, sets a column trigger on
the board's In Progress column, moves the card in to enqueue a workflow run,
adds a comment, captures the live board-room deltas that reach a second viewer
(card_created/card_moved/comment_added via `build_board_delta_publisher`), and
confirms attention items surface in the team-scoped inbox with zero cross-tenant
leakage (`load_inbox_for_team` with a foreign principal returns []). Closes M7.

Code touches: `application/board_poc.py` (`BoardPocReport` + `run_board_poc()`;
walks real application seams — BoardService, dispatch_coding_run, WorkflowService,
set_column_trigger, move_card, comment_card, board delta publisher, inbox loader;
sqlalchemy-free) and `tests/test_board_poc.py` (acceptance walk + determinism/
isolation across two fresh temp DBs; service-generated board/card/column/workflow-run
ids intentionally not compared for equality).

Verification: `tests/test_board_poc.py` — 2 passed. Regressions: 70 board/M7 +
46 workflow + 5 inbox/POC (3 inbox_poc + 2 board_poc) + 20 security, all green.
`makemigrations --check` clean at head `20260831_01` (no schema drift — POC adds
an application module only); compileall, `git diff --check`, and AST sqlalchemy-free
scan clean. Independent fail-closed review: BLOCKERS: none, NON-BLOCKERS: none.
Commit: c7309e8 (pushed).

## M7.6 — Board planning views: filters, group-by, swimlanes, timeline, saved views — verified

Feature: board-level planning surfaces. Cards can be filtered and grouped by
assignee, agent, label, priority, due, or status; a swimlane view groups by
assignee or agent; a timeline view groups cards by due date and marks overdue
buckets. Saved views persist per user with strict owner scoping plus board-access
checks, and both board pages (`/board/{board_id}` and `/boards/{board_id}`) render
the same toolbar and planning fragments. Unknown view/group values fail closed to
the plain Kanban board.

Code touches: pure `application/board_views.py` view model (`build_board_planning_view`
+ owner-scoped `BoardSavedViewService`); `BoardFilterDTO`/`BoardGroupDTO`/
`SavedBoardViewDTO` + `CardDTO.assignee_kind`; `SavedBoardView` entity +
`board_saved_view` table + idempotent migration `20260831_01`; repository/port
saved-view methods + assignee_kind join; saved-view create/open/delete routes +
planning query params on both board routers and templates (`board.html` toolbar,
`board_views.html`, `swimlane.html`, `timeline.html`).

Verification: `tests/test_board_planning_views.py` (4), `test_board_saved_views.py`
(owner scoping, same-board member isolation, blank-name atomicity, migration
fresh/legacy/populated round-trip), `test_board_planning_routes.py` (4 routed
surfaces) — 12 focused passed; board/M7 bounded group — 70 passed; workflow bounded
group — 46 passed. `makemigrations --check` clean at head `20260831_01`; legacy
`20260830_02` downgrade/upgrade + populated downgrade round-trips green; compileall,
`git diff --check`, and AST sqlalchemy-free scan clean. Independent fail-closed
review: BLOCKERS: none, NON-BLOCKERS: none. Commit: bc0e131.

## M7.5 — Move-to-column workflow triggers — verified

Feature: board settings map each active column to an enabled workflow. A real
move into that column starts one `column_move` workflow run through the same
application seam for HTTP and agent-tool mutations; the card shows the live run
status and deep-links to workflow detail. Missing, disabled, stale, cross-board,
or cross-workspace rules fail closed.

Code touches: `ColumnWorkflowRule`/`ColumnMoveRunStatusView`, pure
`ColumnTriggerDTO`, repository rule/claim/bind/status projections, migration
`20260830_02`, shared `application/column_triggers.py`, BoardService and tool move
integration, board settings route/template, and canonical/live card badge render
contexts. The MVCC event key dedupes an immediate retry while allowing a genuine
move-out then move-back to enqueue a new run.

Verification: `tests/test_board_column_triggers.py` — 13 passed; board/M7 bounded
group — 70 passed; workflow bounded group — 46 passed. Fresh, legacy, and
populated downgrade migration paths pass; populated downgrade disables/remaps
`column_move` workflows before restoring the legacy CHECK. `makemigrations
--check` reports head `20260830_02` in sync; compileall, `git diff --check`, and
added-line security scan are clean. A broad run reached 92% before the documented
teardown timeout; its three cached failures all passed in isolation. Independent
review inspected the final diff and reran 13 focused tests but parked before a
verdict; the documented executable fail-closed fallback confirmed both production
move paths use the shared seam and concluded `BLOCKERS: None`, `NON-BLOCKERS:
None`.

## M7.1 — Card detail view and edit — [verified] committed + pushed

Feature: clicking a card opens a detail view that edits title, Markdown
description, assignee, due date, priority, and labels; server-side Markdown
preview; an edit-history audit trail (`card_activity`); live metadata badges on
the board; priority/assignee validation; fail-closed board authorization; and
policy-enforced agent `get_card`/`update_card` tools. Empty optional fields can
be cleared from the UI; an empty title is rejected at the service.

Code touches:
- domain/entities.py (CardView += due_date/priority/labels/activity;
  CardActivityView), dto/board.py (+CardDetailDTO), dto/mappers.py (to_card /
  to_card_detail), domain/ports.py (BoardRepository.update_card,
  set_assignee, list_card_activity).
- infrastructure/models.py (CardModel += due_date/priority/labels; new
  CardActivityModel), infrastructure/repositories.py (hydrate new fields;
  update_card empty-string-clears + per-change activity; set_assignee no-noise
  on no-op; list_card_activity; _parse_labels/_json_labels), migration
  20260826_01_card_detail_metadata.py (idempotent ADD COLUMN + card_activity
  table + legacy builtin-agent tool backfill).
- application/services.py (BoardService.get_card_detail, update_card empty-title
  guard, set_assignee), application/tools.py (board-scoped get_card read +
  update_card write tools).
- api/routers/boards.py (GET/POST /boards/{board_id}/cards/{card_id} with
  require_board_access + _require_card_in_board), templates/card.html (badges +
  title link), templates/card_detail.html.

Verification: tests/test_board_card_detail.py (12 tests), test_agent_tool_policy.py
(+2 tool tests), test_security.py (+1 authz test) — all green; the existing
test_management_cli.py makemigrations --check test stays green; compileall OK;
git diff --check clean; added-line security scan clean. Independent fail-closed
review: BLOCKERS: none. Commit: 33f9874 (pushed).

## M7.2 — Board/column management + board switcher — verified, committed

Feature: dedicated app-shell forms create/rename/archive/restore boards;
workspace-authorized sidebar switcher; board settings add/rename/reorder/archive/
restore columns; archived boards/columns stay hidden from active views while
remaining recoverable; card move dropdown derives from live columns instead of
hardcoded seeded IDs.

Code touches: Board/Column archive state across entities/DTOs/ports/models/repos;
BoardService management operations; workspace/archive access gates; idempotent
migration `20260826_02`; board management routes; navigation board menu; board
index/new/settings templates; column actions menu; 19 focused tests.

Verification: focused + regression board/security/tool gate green;
`makemigrations --check` clean at head `20260826_02`; compileall/diff/security
checks clean; migration legacy upgrade+downgrade round-trip preserves data.
Initial independent review blockers remediated; final fail-closed re-review
BLOCKERS: none, NON-BLOCKERS: none.

Next slice: M7.3 — live board updates over WebSocket.

## M7.3 — Live board updates over WebSocket — verified, committed

Feature: each accessible board has an authorized `board:{board_id}` WebSocket
room. Card create/move/edit/comment publish typed `board_delta` frames to that
room; non-acting viewers apply canonical card/comment fragments directly to the
affected DOM (no reload), card moves physically relocate between columns, and
self-echoes/reconnect replays are deduped. Acting client keeps its whole-board
HTMX feedback. Agent-originated mutations (in-process stub/LLM agents and the
agent WS tool frame) publish via a registry publisher seam wired in
`api/board_live.py`; the standalone MCP process is a separate runtime with no
web ConnectionManager, so its mutations cannot broadcast into the web process's
in-memory rooms (documented boundary).

Code touches: pure `BoardDeltaDTO` in dto/board.py; authorized
`/boards/{board_id}/ws` + create/update broadcasts in routers/boards.py;
move/comment broadcasts in routers/cards.py; static/board_live.js subscriber +
in-place applier wired from board.html; stable comment-{id} fragment identity;
`api/board_live.py` adapter (board_room + board-delta publisher rendering the
canonical `card.html` fragment); tool handler publish hooks in
application/tools.py;
Node DOM-shim tests executing the real client script.

Verification: bounded gate 88 green (live server 10, JS shim, board/security/
tool/MCP regressions); `makemigrations --check` clean at head `20260826_02` (no
schema change); compileall/diff/security checks clean. Initial independent
review flagged a wiring gap (agent-originated mutations didn't broadcast) —
remediated; final fail-closed re-review BLOCKERS: none, NON-BLOCKERS: none.
Committed and pushed as f0a81b7.

## How to resume

1. `cd /home/bilal/Projects/Learning/python/crewspace`
2. `git status --short && git log -10 --oneline --decorate`
3. Confirm `master` and `origin/master` point at the same verified M7.6 product
   commit and the worktree is clean.
4. Re-run the focused M7.6 gate if needed:
   `uv run pytest tests/test_board_planning_views.py tests/test_board_saved_views.py tests/test_board_planning_routes.py -q`
   (last result: 12 passed).
5. Verify schema compatibility if touching models/application boundaries:
   upgrade a fresh temporary DB to head, then run
   `uv run crewspace-manage makemigrations --check`; current head is
   `20260831_01`. Do not export `CREWSPACE_DATABASE_URL` globally.
6. Next implementation: M7.7 — board operating-surface integration POC. Follow
   the M7.7 section in `PLAN_M7_BOARD.md` using the verified RED→GREEN/review/
   commit/push workflow. Preserve shipped M7.1–M7.6 behavior.
7. M7.6 verification context: 12 focused tests, 70 board/M7 regressions, and 46
   workflow regressions passed; migration fresh/legacy/populated paths green;
   independent fail-closed review concluded `BLOCKERS: None` and
   `NON-BLOCKERS: None`.

## M7.4 — Card ↔ coding-run / change-set linkage — verified

Feature: cards durably link to coding runs and show live run/change-set/review
badges with canonical deep links. `spawn_coding_run_from_card` derives team and
requester from authenticated card context, rechecks board/team/repository
authorization, dispatches the run, and links it to the card. Terminal outcomes
annotate through a live run/change-set projection, avoiding duplicate status
writes and making retries idempotent.

Code: `CardRunLink`/`CardRunStatusView`, `card_run_link` model + migration
`20260830_01`, BoardRepository link/status projections, strict pure
`CardRunStatusDTO`, BoardService authorization-scoped link/status methods,
authenticated board tool, board-page status lookup, and compact card badges.

Evidence: `tests/test_board_run_links.py` 9 passed; bounded board/live/change-set
gate 108 passed; fresh-head migration drift clean at `20260830_01`; legacy
upgrade/downgrade round-trip green; compileall/diff/security gates clean;
independent review BLOCKERS: none, NON-BLOCKERS: none. Committed as 6a10d78.

## M6.8 — What shipped

M6.8 adds a unified, team-authorized operational inbox at `/inbox` for work that
needs human attention. It remains a projection over authoritative source records,
not a competing source of truth.

Supported attention kinds (8):

1. approval requests;
2. failed coding runs;
3. timed-out coding runs;
4. disconnected agents with active work;
5. failed workflow runs;
6. pending MCP approvals;
7. requested change-set reviews;
8. stale tasks.

Delivered capabilities:

- Executable `INBOX_RULES` taxonomy and deterministic source-derived item IDs.
- Team-scoped projection, natural deduplication, and idempotent reconciliation.
- Source changes refresh/remove projected items while preserving inbox-local owner
  and acknowledgement state.
- Fail-closed team authorization before projection, replay, or actions; cross-team
  and unknown item access reveals nothing.
- Dedicated `/inbox` app-shell with filters for kind, priority, unread, and
  resolution state.
- Assign, acknowledge, and local resolve actions through a team-keyed `InboxStore`.
- Concrete deep links to coding-run, change-set, workflow, agent conversation,
  MCP-connection, and board detail surfaces.
- Monotonic team-scoped event stream, authorized cursor replay endpoint, browser
  reconnect polling, and one unread definition: unresolved + unacknowledged.
- Seeded integration POC covering all eight kinds across coding runs, change sets,
  workflows, agents, MCP tools, and tasks without production data.
- Public documentation in README.md plus detailed release record at
  `docs/RELEASE_M6.8.md`.
- Discoverable sidebar entry: `📥 Inbox` under Tools, linked to `/inbox` and active
  on the inbox page. This was a post-release usability correction after the user
  observed that the route existed but no UI menu linked to it.

## Main M6.8 files

- `src/crewspace/application/inbox.py` — taxonomy, projection, reconciliation,
  authorization gate, filters/view, and pure item actions.
- `src/crewspace/application/inbox_store.py` — team-keyed inbox-local state.
- `src/crewspace/application/inbox_events.py` — monotonic live/replay contract.
- `src/crewspace/application/inbox_poc.py` — deterministic all-source POC.
- `src/crewspace/api/routers/inbox.py` — app-shell/actions/replay routes.
- `src/crewspace/templates/inbox.html` — inbox UI and replay polling.
- `src/crewspace/templates/layout.html` — sidebar `📥 Inbox` navigation entry.
- `tests/test_inbox_*.py` — 27 focused tests.
- `docs/RELEASE_M6.8.md` — detailed milestone release record.
- `README.md` — reader-friendly latest-milestone summary.

## Verification evidence

Last completed focused gate:

- `uv run pytest tests/test_inbox_*.py -q` → 27 passed.
- `uv run crewspace-manage makemigrations --check` → no changes; models in sync
  with head `20260825_01`.
- `python -m compileall` over touched Crewspace modules → clean.
- `git diff --check` → clean.
- Added-line security scan → no real sinks.
- Final executable acceptance review → all seven acceptance items PASS;
  `BLOCKERS: none`.
- Sidebar regression test renders `/inbox` through the real FastAPI/TestClient path
  and asserts an active `<a href="/inbox">…Inbox</a>` entry; all inbox actions remain
  green.

Browser note: autonomous visual proof was blocked by Chrome's local “Allow remote
debugging” approval prompt. Do not report that as a product blocker: the real HTTP
render path and template assertions passed. The temporary Uvicorn process on port
8007 was terminated cleanly; no server is intentionally left running.

## Recent commits (newest first)

- `9418e3a` — `fix(ui): add operational inbox to sidebar navigation`
- `bce97ac` — `docs: summarize M6.8 operational inbox in README`
- `0da818a` — `docs: mark M6.8 section DONE`
- `6b734bc` — `[verified] feat(M6.8): all-source operational inbox POC
  (slice 7, 7/7 — M6.8 DONE)`; tagged `milestone-m6.8`
- `b675504` — `[verified] feat(M6.8): live inbox replay and unread counts
  (slice 6, 6/7)`
- `9070388` — `[verified] feat(M6.8): deep-link every inbox item (slice 5, 5/7)`
- `2eb8fcf` — `[verified] feat(M6.8): app-shell inbox actions and filters
  (slice 4, 4/7)`
- `c812153` — `[verified] feat(M6.8): authorization prevents cross-tenant leakage
  (slice 3, 3/7)`
- `588480b` — `[verified] feat(M6.8): deterministic dedupe/reconciliation
  (slice 2, 2/7)`
- `410491c` — `[verified] feat(M6.8): inbox taxonomy and projection rules
  (slice 1, 1/7)`

## Working conventions to retain

- Milestone slices use RED → GREEN, bounded tests, migration check, compileall,
  `git diff --check`, added-line security scan, fail-closed review, then one
  verified commit/push per slice.
- Ambiguous authorization, unknown inputs, and cross-tenant access fail closed.
- Never export `CREWSPACE_DATABASE_URL` persistently; it overrides pytest fixture
  databases. Use inline `env VAR=... command` when needed.
- Run Crewspace with `uv run uvicorn crewspace.main:app`; verify with
  `uv run pytest -q` or focused bounded groups.
