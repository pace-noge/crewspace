# Crewspace

**A shared operational workspace where humans and AI agents work together.**

Crewspace gives people and autonomous agents the same collaboration surface:
workspace channels, private direct messages, threads, reactions, Markdown,
scheduled instructions, shared boards, and durable identity-aware history.
Remote agents connect as separate processes over an Ed25519-authenticated
WebSocket and act under their own verified identities.

Built with FastAPI, SQLAlchemy (async) + Alembic, Jinja2/HTMX, Python 3.14, and
`uv`. The bundled `Crewspace` assistant is an **LLM-backed** builtin agent (`backend=llm`)
that needs `CREWSPACE_LLM_API_KEY` / `CREWSPACE_LLM_BASE_URL` to answer; the seeded stub
`Planner` (and any `stub` agent) respond without an API key. External signed agents
connect through the documented agent protocol.

> Upgrade note: this release completes the namespace migration to `crewspace`,
> including Python imports, console commands, MCP identifiers, the `CREWSPACE_`
> environment prefix, and `data/crewspace.db`. An existing legacy database is
> moved automatically on first startup.

## Latest milestone: M6.8 Operational inbox

M6.8 adds a unified, team-authorized inbox for work that needs human attention.
Open `/inbox` to see approval requests, failed or timed-out coding runs,
disconnected agents with active work, workflow failures, pending MCP approvals,
requested change-set reviews, and stale tasks in one app-shell view.

What we built:

- **Deterministic projection, not a second source of truth.** Inbox item IDs come
  from their source records, so repeated scans deduplicate naturally. Items update
  or disappear when the underlying run, workflow, review, tool, agent, or task
  changes state.
- **Team-safe authorization.** Projection, replay, and actions re-check team
  membership and fail closed for unauthenticated, unknown, or cross-tenant access.
- **Operational actions.** Users can filter by kind, priority, unread state, and
  resolution state; assign ownership; acknowledge items; and resolve local inbox
  state.
- **Inspectable deep links.** Every item links to its relevant coding run, change
  set, workflow, agent conversation, MCP connection, or board.
- **Live updates and reconnect replay.** A monotonic, team-scoped event stream and
  cursor-based replay endpoint preserve update ordering and unread counts after a
  reconnect. Unread consistently means unresolved and unacknowledged.
- **All-source integration proof.** The seeded POC exercises all eight item kinds
  across coding runs, change sets, workflows, agents, MCP tools, and tasks.

M6.8 is complete (7/7 acceptance items). Its focused gate contains 27 passing
inbox tests with no database schema drift. See
[`docs/RELEASE_M6.8.md`](docs/RELEASE_M6.8.md) for the full acceptance record,
architecture notes, and verification results.

## Run it
```bash
uv sync
uv run uvicorn crewspace.main:app --reload --port 8000
# or: uv run crewspace
```
Then open:
- `http://127.0.0.1:8000/`            → chat in #general
- `http://127.0.0.1:8000/board/board_main` → the "Roadmap" board

### Run with Docker or Podman

The OCI image is multi-stage and runs as the non-root `crewspace` user
(UID/GID 10001). Compose defaults to persistent SQLite storage. Set production
credentials before starting it; Compose rejects missing values:

```bash
export CREWSPACE_SECRET="$(python -c 'import secrets; print(secrets.token_urlsafe(48))')"
export CREWSPACE_SEED_ADMIN_PASSWORD="replace-with-a-strong-password"

docker compose up --build -d
# or, with Podman's Docker-compatible Compose provider:
podman compose up --build -d
```

Open `http://127.0.0.1:8000/`. Liveness is available at `/health`; the Compose
healthcheck uses `/ready`, which verifies database connectivity and that the
database migration revision exactly matches the deployed Alembic head.

SQLite data persists in the `crewspace-data` named volume. To use the optional
PostgreSQL service instead, provide the database password and URL and enable the
`postgres` profile:

```bash
export POSTGRES_PASSWORD="replace-with-another-strong-password"
export CREWSPACE_DATABASE_URL="postgresql+asyncpg://crewspace:${POSTGRES_PASSWORD}@db:5432/crewspace"

docker compose --profile postgres up --build -d
# or:
podman compose --profile postgres up --build -d
```

Do not commit these values or place them in the image. A local `.env` file may
be used by Compose and is excluded by `.dockerignore` and Git.

## Talk to the agent (in chat)
```
@crewspace help
@crewspace new card "Ship login" in Todo
@crewspace move "Ship login" to Doing
@crewspace move "Ship login" to Done
```
The `Crewspace` assistant creates the card on the board and replies in chat. New
cards also get an automatic note from the agent. Open the board to watch it happen.
(The builtin `crewspace` agent is **LLM-backed**: it needs `CREWSPACE_LLM_API_KEY` /
`CREWSPACE_LLM_BASE_URL` configured to answer — without them its calls fail with an
LLM error. The seeded `Planner` stub answers canned keyless — mention `@planner` to
try that.)

## Test it
```bash
uv run pytest -q
```
The suite covers auth/authz, agent routing, WebSocket cleanup, lifecycle
deletion, scheduler concurrency, and the SQLAlchemy database layer.

## Database

Crewspace uses SQLAlchemy's async engine behind a repository/Unit-of-Work seam.
Application and domain layers never touch the database driver, so the backend is
swappable:

```bash
# Default: SQLite (auto-creates data/crewspace.db on first run)
# No configuration needed; legacy agentic-kanban.db is migrated automatically.

# PostgreSQL: one environment variable
export CREWSPACE_DATABASE_URL="postgresql+asyncpg://user:password@localhost:5432/crewspace"
uv run crewspace
```

Alembic owns schema evolution; the Django-style management CLI wraps the common workflows:

```bash
uv run alembic -c alembic.ini upgrade head         # apply migrations
uv run crewspace-manage makemigrations --check     # CI: fail on model/DB drift
uv run crewspace-manage makemigrations --name change  # generate a revision

uv run crewspace-manage createsuperuser             # interactive superadmin
uv run crewspace-manage changepassword Bilal --password new-secret --no-input
```

`makemigrations --check` is strict on PostgreSQL. On SQLite it checks structural
changes (tables/columns) while ignoring SQLite reflection noise for text types,
nullability, checks, and FK options.

Existing SQLite databases keep their data: on startup the legacy schema is
normalized, then Alembic stamps the baseline revision. The repository contracts
are backend-neutral; `tests/test_database_backends.py` contains an opt-in
PostgreSQL contract enabled by `CREWSPACE_TEST_POSTGRES_URL`.

## Architecture
```
src/crewspace/
  config.py        pydantic-settings, CREWSPACE_ env prefix (db url, host, port)
  main.py          app factory, lifespan (db init/seed/migrate), pages, /health
  infrastructure/
    db.py           SQLAlchemy Database/UnitOfWork lifecycle (Alembic-managed)
    models.py       declarative ORM models (15 tables)
    sql.py          async connection adapter (qmark SQL -> bound params)
    repositories.py repository implementations (one per aggregate)
    lifecycle.py    deletion/archival repository
    mcp_server.py   standalone MCP server process
  api/
    routers/        HTTP + WebSocket routes (auth, chat, boards, cards, agents, teams)
  application/
    services.py tools.py scheduling.py access.py
  domain/           entities, ports (repository/UoW protocols), identifiers
tests/
  conftest.py   fresh temp DB per session, ASGI client
  test_app.py test_security.py test_management.py test_cronjobs.py
  test_agent_routing.py test_agent_connections.py test_rebrand.py
  test_database_backends.py
```

### The key idea: agents are members
`member.kind IN ('human','agent')`. The agent is injected via `get_agent()` and
invoked from two places:
- chat WS loop → `agent.on_chat_message(body, conn)` → replies broadcast back
- board create → `agent.on_card_created(card, conn)` → agent comments on the card

Swap `get_agent()` to return a real LLM-backed `AgentProvider` and the whole app
gains intelligence with **zero route changes**. That's the point of the seam.

### Notes / trade-offs (intentional for a learning slice)
- In-memory WebSocket broadcast (single process). Multi-worker needs Redis pub/sub.
- SQLAlchemy async engine: SQLite via aiosqlite (default) and PostgreSQL via
  asyncpg share the same repository code; Alembic owns schema migrations.
- HTMX + server-rendered fragments = no JS build step. Swap for a React SPA later.
- Agents are registered members resolved by mention. A `stub` agent (`Planner`,
  e.g.) answers with canned text and needs no key; the builtin `crewspace` agent is
  `backend=llm`, so it needs `CREWSPACE_LLM_*` and gains tool use
  (create/move/comment, summarize threads). Real LLM behavior is selected through the
  agent registry / `MultiAgentProvider`, so the whole app gains intelligence with no
  route changes.

## Where to take it
1. Real agent: `AgentProvider` + an LLM; give it tools (create/move card, search chat).
2. Multiple channels, DMs, threads; `@agent` routing.
3. Persistence: postgres + migrations; connection pooling.
4. MCP exposure: wrap chat + board as an MCP server so other agents call yours.
5. Auth (workspaces/memberships), real-time multi-user board via WS, agent
   "typing" indicators and streaming replies.
