# Crewspace — Session Progress (fresh-session handoff)

Last updated: 2026-08-25 (WIB)
Repository: `/home/bilal/Projects/Learning/python/crewspace`
Branch: `master` → `origin/master`
Current pushed HEAD: `9418e3a`
Worktree at handoff: clean

## Current milestone state

- M6.1 through M6.8 are complete.
- M6.7 is DONE 7/7 and tagged `milestone-m6.7` at `a2bec64`.
- M6.8 — Operational inbox — is DONE 7/7.
- M6.8 implementation/release tag: `milestone-m6.8` → `6b734bc`
  (`6b734bcd8af58c54ebc7434fbb18dcd8872a7a61`).
- Follow-up documentation and UI-discoverability fixes are on `master` after the
  milestone tag (latest pushed HEAD `9418e3a`).
- PLAN.md defines no M6.9 or later milestone. Do not invent new scope: agree the
  next milestone and acceptance criteria before implementation.

## How to resume

1. `cd /home/bilal/Projects/Learning/python/crewspace`
2. `git status --short && git log -10 --oneline --decorate`
3. Confirm `master` and `origin/master` point at `9418e3a` (or a newer intentional
   commit) and the worktree is clean.
4. Re-run the focused completed-milestone gate if needed:
   `uv run pytest tests/test_inbox_*.py -q` (last result: 27 passed).
5. Verify schema compatibility if touching models/application boundaries:
   `uv run crewspace-manage makemigrations --check`.
6. Next decision: define M6.9/new milestone scope and acceptance criteria. M6.8
   requires no remaining implementation work.

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
