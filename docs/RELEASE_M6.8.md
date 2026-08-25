# Milestone M6.8 — Operational inbox

Released: 2026-08-25 (WIB)
Tag: `milestone-m6.8`

## Summary

M6.8 adds a unified, team-authorized human-attention inbox over coding runs,
change sets, workflow runs, remote agents, MCP tools, approval requests, and stale
tasks. The inbox is a projection over source records rather than a competing
source of truth: source-derived item identities dedupe deterministically and
reconciliation updates or removes items as their sources change.

## Acceptance (7/7 — DONE)

- [x] Inbox item taxonomy and source-to-item projection rules are documented.
- [x] Items dedupe deterministically and update/resolve with their source record.
- [x] Authorization prevents cross-tenant information leakage.
- [x] Dedicated app-shell inbox supports filter, assign, acknowledge, and resolve.
- [x] Every item deep-links to the relevant run/workflow/tool/review detail.
- [x] Live updates and reconnect replay preserve correct unread counts.
- [x] Integration POC exercises at least one item from each supported source.

## What shipped

1. Executable eight-kind taxonomy and deterministic team-scoped projection.
2. Idempotent reconciliation preserving owner/acknowledgement while source fields refresh.
3. Fail-closed principal/team authorization gate.
4. `/inbox` app-shell with filters and assign/acknowledge/resolve actions.
5. Concrete detail links for every supported item kind, including parent detail IDs.
6. Monotonic team-scoped update stream, cursor replay endpoint, browser reconnect polling,
   and a single unread definition (unresolved + unacknowledged).
7. Seeded integration POC covering all eight kinds across coding runs, change sets,
   workflows, agents, MCP tools, and tasks.

## Verification

- 27 focused M6.8 tests pass (`tests/test_inbox_*.py`).
- `makemigrations --check` reports no model/schema drift.
- `compileall`, `git diff --check`, and the added-line security scan pass.
- Final executable acceptance review: all 7 PASS; BLOCKERS none.

## Architecture notes

- Source records remain authoritative; only owner, acknowledgement, and local resolution
  state live in the inbox store.
- Tenant authorization is checked before projection or replay, and unknown/cross-team
  actions fail closed.
- Live replay is transport-neutral and monotonic; the current UI polls the authorized
  replay endpoint and reconnects from its cursor.
