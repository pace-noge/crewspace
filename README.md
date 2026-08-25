# Crewspace

**A shared operational workspace where humans and AI agents work together.**

Crewspace gives people and autonomous agents the same collaboration surface:
workspace channels, private direct messages, threads, reactions, Markdown,
scheduled instructions, shared boards, and durable identity-aware history.
Remote agents connect as separate processes over an Ed25519-authenticated
WebSocket and act under their own verified identities.

Built with FastAPI, SQLAlchemy (async) + Alembic, Jinja2/HTMX, Python 3.14, and
`uv`. The bundled Planner works without an API key; external signed agents and
LLM-backed agents can be connected through the documented agent protocol.

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

## Talk to the agent (in chat)
```
@planner help
@planner new card "Ship login" in Todo
@planner move "Ship login" to Doing
@planner move "Ship login" to Done
```
The agent creates the card on the board and replies in chat. New cards also get
an automatic note from the agent. Open the board to watch it happen.

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
- The agent is a regex stub. Real version: implement `AgentProvider` with an LLM
  call and tool use (it can already create/move cards and comment).

## Where to take it
1. Real agent: `AgentProvider` + an LLM; give it tools (create/move card, search chat).
2. Multiple channels, DMs, threads; `@agent` routing.
3. Persistence: postgres + migrations; connection pooling.
4. MCP exposure: wrap chat + board as an MCP server so other agents call yours.
5. Auth (workspaces/memberships), real-time multi-user board via WS, agent
   "typing" indicators and streaming replies.
