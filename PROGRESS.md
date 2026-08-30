# Crewspace — Session Progress (fresh-session handoff)

Last updated: 2026-08-25 (WIB)
Repository: `/home/bilal/Projects/Learning/python/crewspace`
Branch: `master` → `origin/master`
Latest product/UI commit before this handoff: `9418e3a`
Handoff state: this PROGRESS.md commit and `origin/master` are synchronized.
Worktree at handoff: clean

## Current milestone state

- M6.1 through M6.8 are complete.
- M6.7 is DONE 7/7 and tagged `milestone-m6.7` at `a2bec64`.
- M6.8 — Operational inbox — is DONE 7/7.
- M6.8 implementation/release tag: `milestone-m6.8` → `6b734bc`
  (`6b734bcd8af58c54ebc7434fbb18dcd8872a7a61`).
- Follow-up documentation and UI-discoverability fixes are on `master` after the
  milestone tag (latest pushed HEAD `9418e3a`).
- M7 — Board as the Agent Operating Surface — is active (2/7 slices).
  `PLAN.md` has the tracker; `PLAN_M7_BOARD.md` is the canonical detailed plan
  and carries the append-only per-slice progress log. Every slice documents its
  user-visible Feature and concrete Code touchpoints, then follows the verified
  RED→GREEN/review/commit/push workflow.

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
Committed and pushed.

## How to resume

1. `cd /home/bilal/Projects/Learning/python/crewspace`
2. `git status --short && git log -10 --oneline --decorate`
3. Confirm `master` and `origin/master` point at the same commit and the worktree
   is clean. `9418e3a` is the latest product/UI commit before the handoff docs.
4. Re-run the focused completed-milestone gate if needed:
   `uv run pytest tests/test_inbox_*.py -q` (last result: 27 passed).
5. Verify schema compatibility if touching models/application boundaries:
   `uv run crewspace-manage makemigrations --check`.
6. Next implementation: M7.4 — Card ↔ coding-run / change-set linkage. Follow
   `PLAN_M7_BOARD.md`; M7.3 is verified and committed; M7.4 is PLANNED.

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
