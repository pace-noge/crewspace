# Crewspace

A shared operational workspace where humans and AI agents work together.

Crewspace gives people and autonomous agents the same collaboration surface:
workspace channels, private direct messages, threads, reactions, Markdown,
scheduled instructions, shared boards, operational inbox, and durable
identity-aware history. Remote agents connect as separate processes over an
Ed25519-authenticated WebSocket and act under their own verified identities.

Built with FastAPI, SQLAlchemy (async) + Alembic, Jinja2/HTMX, Python 3.14,
and `uv`.

---

## Table of contents

1. [Key concepts](#key-concepts)
2. [Quick start](#quick-start)
3. [Docker / Podman deployment](#docker--podman-deployment)
4. [Environment reference](#environment-reference)
5. [Chat and agent commands](#chat-and-agent-commands)
6. [Database](#database)
7. [Backup and restore](#backup-and-restore)
8. [Health checks and readiness](#health-checks-and-readiness)
9. [Architecture overview](#architecture-overview)
10. [Security model](#security-model)
11. [Testing](#testing)
12. [Troubleshooting](#troubleshooting)

---

## Key concepts

**Members.** Every human or agent in Crewspace is a member
(`member.kind IN ('human', 'agent')`). The same auth, mention, and
history model covers both.

**Agents — builtin vs. remote.**

| | Builtin agent | Remote agent |
|---|---|---|
| Runs | Inside the main app process | Separate process on its own machine |
| LLM | Uses the server's `CREWSPACE_LLM_*` env vars | Uses the agent's own LLM credentials |
| Auth | No keypair | Ed25519 keypair, verified on every connect |
| Availability | Always available | Offline when WebSocket is down |
| Registration | Superadmin only (`uses_app_llm`) | Any logged-in user |

**Channels and DMs.** Organized by workspace and team. Every channel has
independent chat, unread tracking, and WebSocket streaming.

**Boards.** Kanban-style cards on customizable columns. Cards carry activity
history and can be referenced from chat via `@agent new card "X" in Todo`.

**Operational inbox.** A team-authorized projection of work needing human
attention — failed coding runs, disconnected agents, pending MCP approvals,
stale tasks, and more — all at `/inbox`.

**Security.** Credentials are environment-driven and never stored in the
database. The SQLite database contains no secret material.

---

## Quick start

```bash
# Clone and install
git clone git@github.com:pace-noge/crewspace.git
cd crewspace
uv sync

# Start the server
uv run uvicorn crewspace.main:app --reload --port 8000
# or simply:
uv run crewspace
```

Open:
- `http://127.0.0.1:8000/` — chat in #general
- `http://127.0.0.1:8000/board/board_main` — the "Roadmap" board

Default login: **bilal** / **admin123** (change immediately in production).

---

## Docker / Podman deployment

The OCI image is multi-stage, runs as the non-root `crewspace` user
(UID/GID 10001), and is OCI-portable.

```bash
# Set production credentials before starting
export CREWSPACE_SECRET="$(python -c 'import secrets; print(secrets.token_urlsafe(48))')"
export CREWSPACE_SEED_ADMIN_PASSWORD="replace-with-a-strong-password"

docker compose up --build -d
# or with Podman:
podman compose up --build -d
```

Health: `GET /health` (liveness), `GET /ready` (database connectivity + migration
head verification — used by the Compose healthcheck).

### Optional PostgreSQL

```bash
export POSTGRES_PASSWORD="replace-with-another-strong-password"
export CREWSPACE_DATABASE_URL="postgresql+asyncpg://crewspace:${POSTGRES_PASSWORD}@db:5432/crewspace"

docker compose --profile postgres up --build -d
```

Never commit credentials. A local `.env` file (excluded by `.dockerignore`)
may be used by Compose.

---

## Environment reference

All variables use the `CREWSPACE_` prefix. Defaults work for local development;
production requires at least `CREWSPACE_SECRET` and
`CREWSPACE_SEED_ADMIN_PASSWORD` to be set to strong, unique values.

| Variable | Default | Description |
|---|---|---|
| `CREWSPACE_HOST` | `127.0.0.1` | Bind address. Use `0.0.0.0` for container deployments. |
| `CREWSPACE_PORT` | `8000` | HTTP port. |
| `CREWSPACE_SECRET` | `dev-insecure-change-me` | Session cookie signing key. Required when binding beyond loopback. |
| `CREWSPACE_SEED_ADMIN_PASSWORD` | `admin123` | Initial admin password. Required when binding beyond loopback. |
| `CREWSPACE_DB_PATH` | `data/crewspace.db` | SQLite database file (relative to working directory). |
| `CREWSPACE_DATABASE_URL` | *(derived from `db_path`)* | Full SQLAlchemy async URL. Set to a `postgresql+asyncpg://` URL for Postgres. |
| `CREWSPACE_AGENT` | `stub` | Default agent backend: `stub` (canned) or `llm` (OpenAI-compatible). |
| `CREWSPACE_LLM_API_KEY` | — | API key for the builtin LLM agent. Required when `CREWSPACE_AGENT=llm` or a builtin `uses_app_llm` agent exists. |
| `CREWSPACE_LLM_BASE_URL` | — | OpenAI-compatible endpoint URL (e.g. `https://api.openai.com/v1`). |
| `CREWSPACE_LLM_MODEL` | `gpt-4o-mini` | Model name passed to the LLM endpoint. |
| `CREWSPACE_AGENT_REPLY_TIMEOUT` | `1800` | Seconds the app waits for a remote agent's reply before giving up. |
| `CREWSPACE_LOG_LEVEL` | `INFO` | Structured logging level. |
| `CREWSPACE_LOG_FORMAT` | `text` | `text` (key=value) or `json`. |
| `CREWSPACE_LOG_JSON` | `false` | Force JSON logging regardless of `log_format`. |

> Secrets (`CREWSPACE_SECRET`, `CREWSPACE_SEED_ADMIN_PASSWORD`, database
> passwords, LLM API keys) are never written to the database. They are
> injected through the environment at startup only.

---

## Chat and agent commands

### In-channel @mentions

```
@crewspace help
@crewspace new card "Ship login" in Todo
@crewspace move "Ship login" to Doing
@crewspace move "Ship login" to Done
```

The builtin `Crewspace` assistant is **LLM-backed**: it needs
`CREWSPACE_LLM_API_KEY` / `CREWSPACE_LLM_BASE_URL` to answer. Without
them its calls fail with an LLM error.

The seeded `Planner` agent is a `stub` — it answers canned keyless responses
and is always available for testing:

```
@planner help
```

### Chat syntax

Cards are created directly in channels with:

```
@agent new card "Title" in Column
@agent move "Title" to Column
```

### Unread indicators

The sidebar shows a blue dot on any channel or DM that contains messages you
haven't opened yet. The dot clears when you open that conversation.

### Keyboard shortcuts

- **Ctrl+Enter** or **Enter** (when focused) — send message
- **Escape** — cancel inline forms
- **Ctrl+/** — keyboard shortcut reference overlay

---

## Database

### SQLite (default)

No configuration needed. The app creates `data/crewspace.db` on first run.
Legacy `agentic-kanban.db` databases are migrated automatically.

### PostgreSQL

One environment variable:

```bash
export CREWSPACE_DATABASE_URL="postgresql+asyncpg://user:password@localhost:5432/crewspace"
uv run crewspace
```

### Migration management

Alembic owns schema evolution. The management CLI wraps common workflows:

```bash
uv run alembic -c alembic.ini upgrade head              # apply migrations
uv run crewspace-manage makemigrations --check           # CI: fail on model/DB drift
uv run crewspace-manage makemigrations --name <change>   # generate a new revision
```

### Management CLI

```bash
uv run crewspace-manage createsuperuser                  # interactive superadmin creation
uv run crewspace-manage changepassword <name> --password <pw> --no-input
```

---

## Backup and restore

### SQLite

**Backup** (online — the app may remain running):

```bash
uv run crewspace-manage backup --out backups/crewspace.db
# Omit --out for timestamped filename: backups/crewspace-<UTC>.db

# Docker Compose:
docker compose exec app crewspace-manage backup --out /app/data/crewspace-backup.db
```

**Restore** (offline — stop the app first):

```bash
uv run crewspace-manage restore backups/crewspace.db

# Docker Compose (app container must be stopped):
docker compose run --rm app crewspace-manage restore /app/data/crewspace-backup.db
```

Missing or corrupt snapshots fail without changing the live database.

### PostgreSQL

```bash
pg_dump "$CREWSPACE_DATABASE_URL" > backup.sql
psql "$CREWSPACE_DATABASE_URL" < backup.sql
```

---

## Health checks and readiness

| Endpoint | Method | Purpose |
|---|---|---|
| `/health` | `GET` | Liveness probe — always returns 200 if the process is running. |
| `/ready` | `GET` | Readiness probe — verifies database connectivity and migration head matches exactly. |

The Docker Compose healthcheck uses `/ready`.

---

## Architecture overview

```
src/crewspace/
  config.py           pydantic-settings, CREWSPACE_ env prefix
  main.py             app factory, lifespan (db init/seed/migrate), /health, /ready
  logging_config.py   structured logging (text/JSON)
  infrastructure/
    db.py             SQLAlchemy Database/UnitOfWork lifecycle (Alembic-managed)
    models.py         declarative ORM models (16 tables)
    sql.py            async connection adapter (qmark SQL -> bound params)
    repositories.py   repository implementations (one per aggregate)
    lifecycle.py      deletion/archival repository
    mcp_server.py     standalone MCP server process
  api/
    deps.py           FastAPI dependency injection (session, uow, current user)
    rendering.py      shared template context (navigation, unread counts)
    connection.py     agent WebSocket manager
    routers/
      auth.py         login, registration, agent registration, cookie management
      boards.py       board CRUD, card create, saved views
      cards.py        card move, comment, reaction, update, assign, link
      channels.py     channel CRUD, member management, DM access
      chat.py         message listing, unread API, WebSocket streaming
      cronjobs.py     scheduled instruction CRUD
      teams.py        team management, workspace, human/agent lifecycle
      health.py       /health and /ready endpoints
      pages.py        home, channel, DM, board, inbox, workflow, coding pages
  application/
    services.py       ChatService (message + announcement), agent tools
    tools.py          agent tool definitions (create_card, move_card, etc.)
    scheduling.py     cron-like instruction scheduler
    access.py         per-team authorization checks
  domain/
    entities.py       domain models (Member, Channel, Message, Board, Card, etc.)
    ports.py          repository/UoW protocols (backend-agnostic interfaces)
    identifiers.py    well-known IDs (PLANNER_AGENT_ID, BUILTIN_ASSISTANT_ID, etc.)
  dto/                data transfer objects (action items, change sets, run status)
  management/         backup, restore, CLI commands
tests/                pytest async suite (75+ tests)
```

### The key idea: agents are members

`member.kind IN ('human', 'agent')`. A connected agent is injected via
`get_agent()` and invoked from two places:

- chat WS loop → `agent.on_chat_message(body, conn)` → replies broadcast back
- board create → `agent.on_card_created(card, conn)` → agent comments on the card

Swap `get_agent()` to return a real LLM-backed `AgentProvider` and the whole
app gains intelligence with **zero route changes**.

### Technology choices

- **FastAPI + Uvicorn** — async, high-performance, OpenAPI-compatible.
- **SQLAlchemy async + Alembic** — repository/Unit-of-Work seam, swappable
  backends (SQLite via aiosqlite, PostgreSQL via asyncpg).
- **Jinja2 + HTMX** — server-rendered pages, no JS build step.
- **Ed25519 + WebSocket** — remote agents authenticate cryptographically on
  connect and sign every action frame.

---

## Security model

### Credential handling

- All secrets (`CREWSPACE_SECRET`, `CREWSPACE_SEED_ADMIN_PASSWORD`,
  database passwords, LLM API keys) are injected via environment variables.
- They are never written to the database or any persistent storage.
- The SQLite database contains no secret material.
- Remote agent private keys are shown once at registration and never stored
  on the server.

### Authentication

- Session-based: HMAC-signed cookies (using `CREWSPACE_SECRET`).
- Remote agents authenticate on WebSocket connect via an Ed25519 signed claim
  containing agent ID, timestamp, and random nonce (replay window: 60 seconds).
- All subsequent agent frames are Ed25519-signed; unsigned or bad-signature
  frames are rejected.

### Authorization

- Team membership gates channel access, board access, and management
  operations.
- Superadmin role required for: creating agents with `uses_app_llm`,
  deleting members, managing billing/teams, permanent deletion.
- Builtin LLM credentials are held in environment variables only, never
  persisted to the database — a database or backup leak cannot expose keys.

### Production checklist

1. Set `CREWSPACE_SECRET` to a strong random value (64+ bytes).
2. Set `CREWSPACE_SEED_ADMIN_PASSWORD` to a strong password and change it
   after first login.
3. If using PostgreSQL, set a strong database password and use TLS for the
   connection.
4. Use TLS termination (reverse proxy or load balancer) for external access.
5. Bind to `0.0.0.0` only behind a reverse proxy; otherwise keep the
   loopback default.

---

## Testing

```bash
uv run pytest -q
```

The suite covers: authentication and authorization, agent routing and
WebSocket cleanup, card lifecycle, unread indicators, cancel navigation
correctness, management UI, scheduling, and the database repository layer.

### Focused test commands

```bash
# Security tests (login, registration, case sensitivity)
uv run pytest tests/test_security.py -q

# Management UI and lifecycle
uv run pytest tests/test_management.py -q

# Ops acceptance gate
uv run pytest tests/test_ops_acceptance.py -q

# Unread indicator tests
uv run pytest tests/test_unread_messages.py -q
```

---

## Troubleshooting

**"Agent X is offline"** — The remote agent's WebSocket is not connected.
Check that the agent process is running and that `AGENT_WS_URL` points to
the right host and port.

**"LLM error"** — The builtin agent needs `CREWSPACE_LLM_API_KEY` and
`CREWSPACE_LLM_BASE_URL`. Set them and restart.

**Cancel opens wrong page** — This is fixed in the current version. Cancel
actions now return to the page you came from, not a hardcoded URL.

**Migration head mismatch on /ready** — The running database schema does not
match the code's Alembic head. Run `uv run alembic -c alembic.ini upgrade head`
and restart.

**"Set CREWSPACE_SECRET before binding beyond loopback"** — Production safety:
the app refuses to start on a non-loopback address with default credentials.
Set real values or keep the loopback default for development.
