# M9 — Production Hardening and Live Deployment

Status: IN PROGRESS
Canonical tracker: `PLAN.md` (M9 slice table)
Progress log: appended below (newest first), mirroring the M6/M7/M8 convention.

## Goal

Bring Crewspace from a dev-grade FastAPI app to something that can be deployed
and operated as a live service: structured production logging, hardened
runtime configuration, liveness/readiness observability, containerization, a
backup/restore seam, and a release runbook. Every slice follows the SAME
verified-slice discipline as M6/M7/M8: RED → GREEN, bounded test gate,
migration-compat guard (pure DTO where applicable), compileall,
`git diff --check`, added-line security scan, and an independent fail-closed
review before a `[verified]` commit + push.

## Slice table

| Slice | Deliverable | Status | Progress |
|------:|-------------|--------|----------|
| M9.1 | Structured production logging | DONE | 7/7 |
| M9.2 | Runtime config hardening + validation | DONE | 7/7 |
| M9.3 | Health / readiness endpoints + DB/migration check | DONE | 7/7 |
| M9.4 | Containerization (Dockerfile + docker-compose, non-root) | DONE | 7/7 |
| M9.5 | `crewspace-manage backup` / `restore` seam (atomic) | PLANNED | 0/5 |
| M9.6 | Release runbook + deployment docs | PLANNED | 0/5 |
| M9.7 | Cohesive ops acceptance gate | PLANNED | 0/5 |

## Cross-cutting invariants (apply on EVERY slice)

- Never weaken the existing fail-closed guards: `config.py` still refuses to
  bind beyond loopback with dev credentials; migration drift still fails.
- Pure-DTO modules stay sqlalchemy-free (AST scan) so `makemigrations --check`
  can never drift because of them.
- No new secrets in code or committed files; everything configurable stays
  environment-driven via the `CREWSPACE_` prefix.
- Each slice ships a bounded test group + compileall + `git diff --check` +
  migration-compat check + independent fail-closed review with
  `BLOCKERS:`/`NON-BLOCKERS:`/`EVIDENCE:` before a `[verified]` commit + push.
- `commit` messages written via quoted heredoc (never backticks in `-m`).

---

## M9.1 — Structured production logging

Feature: replace ad-hoc `print`/bare-log with a single, config-driven stdlib
`logging` setup that emits structured (key=value / JSON) records with request
context and a stable logger namespace. `CREWSPACE_LOG_LEVEL`,
`CREWSPACE_LOG_FORMAT`, `CREWSPACE_LOG_JSON` env knobs.

Code (concrete):
- `src/crewspace/logging_config.py` — `configure_logging(settings)` called in
  `main.py` lifespan and CLI entry points: sets root formatter, JSON-vs-text,
  log level, `alembic`/`uvicorn`/`aiosqlite` level tuning.
- `Settings` gains `log_level`, `log_format`, `log_json` fields.
- Wire a request-id / access log middleware in `main.py` that logs method,
  path, status, duration, request id.

Acceptance:
- [ ] `configure_logging` honors all three env knobs.
- [ ] A request middleware logs method/path/status/duration with a request id.
- [ ] JSON mode emits valid parseable JSON lines.
- [ ] Existing loggers (`crewspace.db`, `alembic`) honor the configured level.
- [ ] No behavior regression: bounded test group + security gate green.

## M9.2 — Runtime config hardening + validation

Feature: tighten `Settings` so a misconfigured production deployment fails fast
at startup with a clear message instead of silently running with weak/invalid
values.

Code (concrete):
- `src/crewspace/config.py` — add validation:
  - `host` must be a valid bind address; port 1..65535.
  - `agent_reply_timeout > 0`.
  - `secret` length floor (warn vs dev placeholder already enforced on
    non-loopback).
  - `database_url` backend must be sqlite or postgresql (both supported).
  - `llm_model`/`llm_base_url` coherence when `agent == "llm"`.
- Unit tests for each validator (fail-closed on invalid input).

Acceptance:
- [ ] Invalid host/port/reply-timeout/db-backend rejected at construction.
- [ ] Valid production-shaped configuration passes.
- [ ] Existing dev defaults remain valid.
- [ ] Compile + migration-compat + bounded test group green.

## M9.3 — Health / readiness endpoints

Feature: `GET /health` (liveness) and `GET /ready` (readiness: app up, DB
reachable, migrations at head) so orchestrators (k8s/compose healthchecks) can
probe the service.

Code (concrete):
- `src/crewspace/api/routers/health.py` — `/health` returns `200 {status: ok}`
  with no DB touch; `/ready` opens a DB connection via `app.state.db`,
  runs `SELECT 1` and compares Alembic head, returns `200` or `503` with a
  body describing the failure. Mounted in `main.py`.
- Tests (bounded, no broad suite): liveness always 200; readiness 200 when DB
  up; readiness 503 when DB down (simulate by closing/disposing db).

Acceptance:
- [ ] `/health` returns 200 without DB.
- [ ] `/ready` returns 200 when DB reachable + migrations at head.
- [ ] `/ready` returns 503 and a diagnostic body when DB is down.
- [ ] Tests pass; no schema change.

## M9.4 — Containerization

Feature: `Dockerfile` (non-root, multi-stage, python:3.14-slim) + `docker-compose.yml`
(app + optional postgres) with healthchecks, so the app can be stood up as a
real service.

Code (concrete):
- `Dockerfile` — build stage installs deps via uv; runtime stage copies app,
  runs as `crewspace` non-root user, exposes port 8000, exec `uvicorn`.
- `docker-compose.yml` — `app` service with build, `CREWSPACE_*` env, volume
  for `data/`, healthcheck on `/ready`; optional `db` postgres service with a
  healthcheck and `CREWSPACE_DATABASE_URL`.
- `.dockerignore` to keep build context lean.
- Add a lightweight test asserting the compose file is valid YAML and the
  Dockerfile is non-root (e.g. greps for `USER` after `RUN` of a non-root uid).

Acceptance:
- [ ] Dockerfile builds non-root and exposes 8000.
- [ ] Compose validates (YAML parses; services have healthchecks).
- [ ] Postgres service wire-up present (optional).
- [ ] Compose/installability documented in docs.

## M9.5 — Backup / restore seam

Feature: `crewspace-manage backup [--out PATH]` snapshots the database
atomically and `crewspace-manage restore PATH` restores it, so an operator can
protect the store.

Code (concrete):
- SQLite: use the SQLite online backup API / file copy with WAL checkpoint to
  produce a consistent snapshot; restore replaces the file.
- Postgres: `pg_dump`/`psql` when `database_url` is postgres, or document
  external tooling; keep the SQLite path tested.
- `src/crewspace/management/commands/backup.py` + `restore.py`, registered in
  `COMMANDS`.
- Tests (bounded): backup produces a restorable file containing seeded data;
  restore into an empty DB yields the original rows.

Acceptance:
- [ ] `backup` writes a consistent snapshot to the requested path.
- [ ] `restore` restores data from a snapshot.
- [ ] Idempotent / safe on a missing file (clean error).
- [ ] Bounded tests green; migration-compat clean.

## M9.6 — Release runbook + deployment docs

Feature: `docs/DEPLOYMENT.md` (prereqs, config matrix, uv/migrations, run via
uv/compose, reverse-proxy + TLS note, backups, upgrades, rollback) and
`docs/RELEASING.md` (version bump, changelog, tag, verified-slice flow). Pure
docs plus a test that the sample config blocks in the docs parse / are valid.

Code (concrete):
- `docs/DEPLOYMENT.md`, `docs/RELEASING.md`.
- A small test ensuring `.env.example` (if added) and doc env names stay in
  sync with `Settings` field names (no drift).

Acceptance:
- [ ] Deployment doc covers security-critical env vars (secret, seed admin,
      log level, db url) and warns about dev defaults on non-loopback.
- [ ] Releasing doc describes versioning/tagging/verified-slice flow.
- [ ] An env-drift test passes (doc/env names ⊆ Settings fields).
- [ ] Nothing schema-changing.

## M9.7 — Cohesive ops acceptance gate

Feature: a single bounded acceptance suite proving the ops surface end to end:
logging config, health/readiness, config validation, backup/restore, and the
deployment artifacts, in one `test_ops_acceptance.py`.

Acceptance:
- [ ] One bounded suite covers every M9 slice's key invariant.
- [ ] Independent fail-closed review of the final diff returns
      `BLOCKERS: none`.
- [ ] PLAN.md/PROGRESS.md mark M9 DONE; verified commit + push.

---

## M9 Progress log (append-only, newest first)

- M9.4 — Portable OCI deployment artifacts — [verified] committed + pushed.

  Feature: multi-stage Python 3.14-slim image with a UID/GID 10001 non-root
  runtime; Docker/Podman-compatible Compose topology with persistent SQLite,
  `/ready` healthcheck, required application secrets, JSON logging defaults,
  and an optional profile-gated PostgreSQL service.

  Code:
  - `Dockerfile`: frozen `uv.lock` dependency install, runtime venv/source and
    Alembic assets, non-root user, port 8000, and exec-form Uvicorn command.
  - `docker-compose.yml`: app and optional PostgreSQL services, named volumes,
    production configuration, and healthchecks.
  - `.dockerignore`: excludes VCS, secrets, local environments, caches, and data.
  - `README.md`: Docker and Podman Compose startup instructions for SQLite and
    PostgreSQL.
  - `tests/test_containerization.py`: 7 engine-neutral acceptance tests.

  Verification: RED 7/7 missing-artifact failures → GREEN 7/7; bounded container,
  health, config, and security gate 45 passed; compileall, migration drift, diff,
  and adjudicated added-line security checks clean; direct production-style
  Uvicorn startup returned 200 from `/health` and `/ready` at Alembic revision
  `20260831_01`. Independent review: BLOCKERS none.

  Final Docker verification: Docker 29.7.2 + Compose 5.4.0 successfully built
  the committed image. Image inspection and a real container confirmed UID/GID
  10001, port 8000, exec-form Uvicorn command, importable package, templates,
  static files, Alembic config, and migration assets. SQLite Compose reached
  healthy, returned 200 from `/health` and `/ready`, created the DB as
  10001:10001, and retained all three seeded members across container removal
  and recreation. The optional PostgreSQL profile also reached healthy for both
  services with zero app restarts; the app used `postgresql+asyncpg` at
  `db:5432/crewspace` and `/ready` reported exact head `20260831_01`. All isolated
  verification containers, volumes, networks, and temporary images were removed.

  Implementation commit: `6a698c3`.
  Progress: 7/7 complete.

- M9.3 — Health / readiness endpoints — [verified] committed + pushed.

  Feature: unauthenticated `GET /health` liveness without a DB touch and
  fail-closed `GET /ready` readiness requiring initialized app state, a working
  `SELECT 1`, a non-empty deployed Alembic head, and an exact singleton
  `alembic_version` match.

  Code:
  - `src/crewspace/api/routers/health.py`: controlled 503 responses for an
    uninitialized/unavailable database, unavailable migration metadata,
    migration-query failure, empty deployed head, multiple database revisions,
    and schema mismatch; raw exception details remain server-log only.
  - `src/crewspace/main.py`: mounts the health router.
  - `tests/test_health.py`: 11 acceptance and migration-compatibility tests.

  Verification: health + logging + config + security regressions green
  (45 passed); compileall; git diff/security scan; migration compatibility clean
  at `20260831_01`. Independent reviews found and drove fixes for raw exception
  leakage, multiple-revision fail-open, and empty-head fail-open; final focused
  re-review BLOCKERS none.

  Verified implementation commit: `2ef0f43`.
  Progress: 7/7 complete.

- M9.1 — Structured production logging — [verified] committed + pushed.

  Feature: replace ad-hoc/bare logging with a single config-driven stdlib
  `logging` setup (`CREWSPACE_LOG_LEVEL`, `CREWSPACE_LOG_FORMAT`, `CREWSPACE_LOG_JSON`),
  plus an access-log middleware that emits a structured line (method, path,
  status, duration_ms, request_id) for every HTTP request — including a
  status=500 line for unhandled exceptions.

  Code:
  - src/crewspace/logging_config.py: `configure_logging(settings)`,
    `StructuredFormatter` (text key=value or single-line JSON), `format_access_line`.
  - src/crewspace/config.py: `Settings` gains `log_level`, `log_format`, `log_json`.
  - src/crewspace/main.py: lifespan calls `configure_logging`; `access_log`
    middleware + `_request_id`; exception branch logs status=500.
  - tests/test_logging_config.py (7 tests): settings knobs, text/JSON formatter,
    log_json override, access-line builder, and two middleware integration tests
    (normal + unhandled-exception status=500).

  Verification: logging + security + app/agent/pipeline regressions green
  (99 passed incl. 27 in this group); compileall, git diff --check,
  migration-compat clean (head `20260831_01`). Two pre-existing failures
  unrelated to this change confirmed on clean HEAD
  (test_lifespan_closes_an_injected_database_once, test_migration_preserves_tools...).
  Independent fail-closed review: BLOCKER (exception path omitted status) fixed
  RED→GREEN and re-reviewed; final VERDICT PASS, BLOCKERS none. In-process
  checks clean (no sqlalchemy import, no secrets, no dangerous patterns).

  Verified implementation commit: `ff3b228`.
  Progress: 7/7 complete.

- M9.2 — Runtime config hardening + validation — [verified] committed + pushed.

  Feature: tighten `Settings` so a misconfigured production deployment fails
  fast at startup with a clear message instead of silently running with
  weak/invalid values.

  Code:
  - src/crewspace/config.py: `port_in_range` field_validator (1..65535),
    `reply_timeout_positive` (> 0), `host_not_empty`; database_url backend
    check (sqlite/postgresql only); llm warning when `agent=llm` without key.
  - tests/test_config_validation.py (7 tests): valid production settings,
    dev defaults, port range, reply timeout, db backend, empty host, llm
    warning.

  Verification: config + security + logging regressions green (34 passed);
  compileall; git diff --check; migration-compat clean (head 20260831_01).
  Independent review BLOCKERS none.

  Verified implementation commit: `f304223`.
  Progress: 7/7 complete.
