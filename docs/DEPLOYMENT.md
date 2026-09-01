# Deployment

This runbook covers production deployment of Crewspace: prerequisites, the
configuration matrix, database migrations, running via `uv` or Compose, a
reverse proxy with TLS, backup, restore, upgrades, and rollback.

Crewspace is a FastAPI application (async SQLAlchemy + Alembic) packaged as a
multi-stage OCI image that runs as the non-root `crewspace` user (UID/GID
10001). It uses the `CREWSPACE_` environment prefix for all configuration.

## Prerequisites

- Python 3.14 (for a `uv` run) **or** Docker / Podman with the Docker-compatible
  Compose provider (for a container deployment).
- A writable location for the SQLite database file, or a PostgreSQL server.

## Configuration matrix

Every setting is an environment variable. The table lists the production-worthy
variables; the authoritative field list lives in `src/crewspace/config.py`
(`Settings`), and a CI test (`tests/test_docs_deploy_release.py`) fails if any
`CREWSPACE_*` name mentioned in this document drifts from a real Settings field.

| Variable | Required | Default | Purpose |
| --- | --- | --- | --- |
| `CREWSPACE_SECRET` | **yes (non-loopback)** | `dev-insecure-change-me` | Signs session cookies. **Must be a long random secret in production.** |
| `CREWSPACE_SEED_ADMIN_PASSWORD` | **yes (non-loopback)** | `admin123` | Initial password for the seeded admin (`user_bilal`). **Change after first login.** |
| `CREWSPACE_DATABASE_URL` | no | `sqlite+aiosqlite:///data/crewspace.db` | SQLAlchemy URL. Allowed backends: `sqlite+` or `postgresql+`. |
| `CREWSPACE_DB_PATH` | no | `data/crewspace.db` | SQLite file path used when `CREWSPACE_DATABASE_URL` is unset. |
| `CREWSPACE_HOST` / `CREWSPACE_PORT` | no | `127.0.0.1` / `8000` | Bind address and port. |
| `CREWSPACE_AGENT` | no | `stub` | `stub` or `llm` agent provider. |
| `CREWSPACE_LLM_API_KEY` | conditional | — | LLM credential; needed for the builtin `crewspace` assistant when `CREWSPACE_AGENT=llm`. |
| `CREWSPACE_LLM_BASE_URL` | no | — | LLM API base URL. |
| `CREWSPACE_LLM_MODEL` | no | `gpt-4o-mini` | LLM model name. |
| `CREWSPACE_AGENT_REPLY_TIMEOUT` | no | `1800.0` | Seconds a remote agent has to reply before the app reports it timed out. |
| `CREWSPACE_LOG_LEVEL` | no | `INFO` | Logging verbosity. |
| `CREWSPACE_LOG_FORMAT` | no | `text` | `text` (key=value) or `json`. |
| `CREWSPACE_LOG_JSON` | no | `false` | Shorthand forcing JSON logging regardless of `CREWSPACE_LOG_FORMAT`. |

### WARNING — dev defaults on a non-loopback bind

Do **not** set `CREWSPACE_HOST` to a non-loopback address (for example `0.0.0.0`)
while `CREWSPACE_SECRET` or `CREWSPACE_SEED_ADMIN_PASSWORD` still have their
development defaults. `Settings` refuses to start in that state:

```
Set CREWSPACE_SECRET and CREWSPACE_SEED_ADMIN_PASSWORD before binding beyond loopback
```

If you must expose Crewspace beyond loopback (which is what production does),
generate strong secrets first. Compose does this automatically via `:?` required
variables — it will refuse to start the `app` service until
`CREWSPACE_SECRET` and `CREWSPACE_SEED_ADMIN_PASSWORD` are set.

## Database migrations

Alembic owns the schema and runs automatically at startup via
`Database.create` → `_upgrade_schema`. You normally never run migrations
manually in a container; the app applies them on boot.

For an explicit uv deployment:

```bash
uv run alembic -c alembic.ini upgrade head
uv run crewspace-manage makemigrations --check   # CI: fail on model/DB drift
```

Generate a new revision:

```bash
uv run crewspace-manage makemigrations --name my_change
```

`makemigrations --check` is strict on PostgreSQL and checks structural changes
on SQLite.

## Run via uv

```bash
uv sync
export CREWSPACE_SECRET="$(python -c 'import secrets; print(secrets.token_urlsafe(48))')"
export CREWSPACE_SEED_ADMIN_PASSWORD="YOUR_ADMIN_PASSWORD"
uv run uvicorn crewspace.main:app --host 0.0.0.0 --port 8000
# or: uv run crewspace
```

## Run via Compose

The OCI image is multi-stage and runs as the non-root `crewspace` user. Compose
defaults to persistent SQLite storage in the `crewspace-data` named volume and
uses `/ready` (which verifies the database migration revision) as its healthcheck.

```bash
export CREWSPACE_SECRET="$(python -c 'import secrets; print(secrets.token_urlsafe(48))')"
export CREWSPACE_SEED_ADMIN_PASSWORD="YOUR_ADMIN_PASSWORD"
docker compose up --build -d
# or with Podman's Docker-compatible Compose provider:
podman compose up --build -d
```

Optional PostgreSQL backend (profile-gated `db` service):

```bash
export POSTGRES_PASSWORD="YOUR_POSTGRES_PASSWORD"
export CREWSPACE_DATABASE_URL="postgresql+asyncpg://crewspace:${POSTGRES_PASSWORD}@db:5432/crewspace"
docker compose --profile postgres up --build -d
```

Never commit these values or place them in the image. A local `.env` file used
by Compose is excluded by `.dockerignore` and Git.

## Reverse proxy + TLS

Terminate TLS at a reverse proxy (Caddy, nginx, or a load balancer) and forward
to the app's `CREWSPACE_PORT`. If the remote-agent WebSocket (`/agents/ws`) or
chat WebSockets are used, the proxy must support WebSocket upgrades and use
`wss://` so the bearer tokens and frame contents do not travel in cleartext (see
`docs/AGENT_PROTOCOL.md`). Connect to `127.0.0.1` only, or through the proxy; do
not expose the raw port directly on the internet without a proxy.

Health check endpoints:

- `GET /health` — liveness; no database dependency.
- `GET /ready` — readiness; fails closed unless the database is reachable and
  the deployed Alembic revision exactly matches the head.

## Backup

SQLite backups use the online backup API and atomically publish an
integrity-checked snapshot. The app may stay online:

```bash
uv run crewspace-manage backup --out backups/crewspace.db
# omitting --out creates backups/crewspace-<UTC timestamp>.db

# Compose deployment:
docker compose exec app crewspace-manage backup --out /app/data/crewspace-backup.db
```

For PostgreSQL use `pg_dump`; the Crewspace backup command rejects PostgreSQL
URLs with that guidance:

```bash
pg_dump "$CREWSPACE_DATABASE_URL" > crewspace.dump
```

## Restore

Restore is an offline operation: stop Crewspace first so no process retains the
old database or WAL state. The command validates the snapshot before atomically
replacing the configured database and removes stale WAL/SHM sidecars.

```bash
uv run crewspace-manage restore backups/crewspace.db

# Compose deployment (the app container must be stopped):
docker compose run --rm app crewspace-manage restore /app/data/crewspace-backup.db
```

For PostgreSQL:

```bash
psql "$CREWSPACE_DATABASE_URL" < crewspace.dump
```

A missing or corrupt snapshot fails without changing the live database.

## Upgrades

1. Pull the new image / source.
2. Take a backup first (see Backup).
3. Start the new version. Alembic applies pending migrations on startup.
4. Confirm readiness via `GET /ready` on the healthcheck port.

## Rollback

1. Stop the new instance.
2. Restore the pre-upgrade backup (see Restore).
3. Start the previous version. Because the database was restored to the
   pre-upgrade schema, the previous version boots against the schema it expects.
