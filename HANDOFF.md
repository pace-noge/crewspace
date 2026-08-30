# HANDOFF — Crewspace restructure (clean layered architecture)

Status: PAUSED mid-restructure, but LEFT IN A GREEN, RUNNABLE STATE.
15 pytest tests pass (stable). Live boot + WebSocket agent loop + signed Ed25519
agent auth verified working end-to-end (agent connects, signs, creates real board
cards, and cannot impersonate another member).

Goal of the work: restructure the working flat app into layered clean
architecture (api / application / domain / infrastructure) with SOLID principles
and a DTO boundary so the database can be swapped (sqlite -> postgres) without
touching routes/services/agents. The user explicitly wanted DTOs for DB-swap.

## Key docs
- `docs/AGENT_PROTOCOL.md` — the full wire contract for building an agent in ANY
  language (Ed25519 auth, frame protocol, tool catalog, LLM-connected example).
  Start with its §0 "What an agent can (and cannot) do" summary.

## What is DONE (working, tested)
- Full layered rewrite completed and verified:
  - domain/      entities.py (Member/Channel/Message/Board/Column/Card/Comment
                 + composed views), ports.py (AgentProvider, ToolRunner,
                 ChatRepository, BoardRepository, UnitOfWork protocols),
                 identifiers.py (stable seeded ids)
  - dto/         board.py (Comment/Card/Column/Board DTOs), messages.py
                 (MessageDTO), mappers.py (view -> DTO)
  - application/ tools.py (ToolRegistry implementing ToolRunner; 6 tools:
                 create_card, move_card, comment_card, find_card, list_columns,
                 post_message), services.py (ChatService, BoardService returning DTOs)
  - infrastructure/ db.py (SCHEMA, seed, Database, SqliteUnitOfWork),
                 repositories.py (Sqlite ChatRepository + BoardRepository),
                 agents/ (StubAgent + LLMAgent both implementing AgentProvider;
                 LLMAgent does OpenAI-compatible function calling over the
                 Tool Registry via the injected AsyncOpenAI client),
                 mcp_server.py (MCP exposure of the same Tool Registry; stdio/
                 sse/streamable-http via the `crewspace-mcp` script)
  - api/         deps.py (DI: get_uow, get_agent, get_registry, service factories),
                 connection.py (WebSocket ConnectionManager),
                 rendering.py (Jinja instance),
                 routers/ chat.py, boards.py, cards.py, pages.py, tools.py
  - main.py      app factory + lifespan (Database.create -> app.state.db)
  - templates/   layout.html (shared shell + left sidebar nav), chat.html,
                 board.html, card.html, column.html, comment.html (HTMX;
                 attribute access). Move/create re-render columns via
                 hx-swap-oob so a moved card leaves its source column live.
- Old flat modules DELETED: board.py, deps.py, state.py, ws.py, agent.py, db.py
  at the package root (no longer exist — do not recreate them).
- Tests updated to new API: conftest uses `Database.create` + `app.state.db`;
  test_app.py endpoints unchanged from before.

## Run / verify
    cd /home/bilal/Projects/Learning/python/crewspace
    uv sync
    uv run pytest -q            # -> 6 passed
    uv run uvicorn crewspace.main:app --reload --port 8000
    # chat: http://127.0.0.1:8000/   board: http://127.0.0.1:8000/board/board_main
    # talk to the default agent in chat: @crewspace new card "X" in Todo|Doing|Done

## Key design decisions (so you don't re-litigate)
- DB-swap seam: `infrastructure/db.py` is the ONLY sqlite-aware module. Swap =
  reimplement `SqliteUnitOfWork` + the two repos against asyncpg, keep the same
  `Database`/`UnitOfWork` surface + domain protocols. Nothing else changes.
- DTO boundary: api never sees entities/rows; services return DTOs (pydantic),
  mapped from domain views in dto/mappers.py.
- Dependency direction: api -> application -> domain (ports). infrastructure
  implements domain ports. No layer imports a layer "above" it.
- Agent acts ONLY via ToolRunner (tools), never touches storage directly. This
  is what makes the same tool set reusable by MCP later (roadmap M2).
- UnitOfWork per request via `Database.uow()` context manager (commits/rolls back).
- WebSocket uses its own UoW dependency (`_ws_uow` in chat.py) because FastAPI
  cannot inject `Request` into a WebSocket endpoint. The HTTP routes use
  `get_uow(request)` from deps.py.

## Known rough edges (non-blocking, can be polished later)
- A `data/crewspace.db` sqlite file is created on run (gitignored? check
  .gitignore). Tests use a temp file.
- Pyright has some benign `reportOptionalSubscript`/undefined-attr warnings on
  aiosqlite `Row` access and pydantic `TemplateResponse` kwargs; runtime is fine.
  Not worth chasing for a learning project.
- `TestClient` emits a StarletteDeprecationWarning about httpx; harmless.
- LLMAgent is IMPLEMENTED (M1 done): OpenAI-compatible function calling over
  the Tool Registry. `CREWSPACE_AGENT=stub` is still the default (keyless). With
  `CREWSPACE_AGENT=llm` + `CREWSPACE_LLM_API_KEY` (and optional `CREWSPACE_LLM_BASE_URL` for any
  OpenAI-compatible endpoint, `CREWSPACE_LLM_MODEL`), the agent thinks via the model.
  The LLM client is injectable for tests (no network/key in CI).
- Tool schemas in application/tools.py are now full JSON Schema (object +
  properties + required) so they work for both function calling and MCP (M2).
- UI known-fixed bugs (see git status / working tree): chat avatar rendered as a
  separate text node (no longer breaks on null avatar); new cards land at the
  BOTTOM of a column; moving a card (dropdown OR drag-and-drop) re-renders the
  WHOLE board (`board_fragment.html`) and swaps `#board-wrap` innerHTML, so no
  phantom copy is left in the old column; nav is a LEFT SIDEBAR (layout.html).
- ROOT CAUSE of all three UAT failures (move didn't apply, + button wiped the
  board, drag-drop did nothing): HTMX was loaded from a CDN
  (`https://unpkg.com/...`) that is BLOCKED in the UAT/offline network, so
  `window.htmx` was undefined and every `hx-*` attribute was inert (the browser
  fell back to native form submits → full-page reloads). FIXED by vendoring
  `htmx.min.js` into `src/crewspace/static/` and serving it at
  `/static/htmx.min.js` (mounted in main.py). No external CDN dependency now.
- Drag-and-drop: FIXED — the drop handler used `htmx.ajax(..., {source: fd})`
  where `fd` was a FormData; htmx's `source` must be a DOM *element*, so the
  POST never fired. Now uses `values: {column_id}` and swaps `#board-wrap`.
- Planner announces board changes in chat: `BoardService`/`ChatService.announce`
  persists a message from `agent_planner` on card create + move, and the board
  routers broadcast it over the WS channel (so it appears live + in history).
  The agent's own `create_card`/`move_card` tools bypass the HTTP routers, so no
  double-announce.

## Slice A–D: multi-user + registerable bots/agents (DONE)

The user wanted real auth (login/password + RBAC) for humans AND a way to
register bot/agent members, with agents eventually running on OTHER machines
(many of them: coder, reviewer, planner). Inspired by Buzz by Block
(github.com/block/buzz): humans + agents are first-class members; a remote
agent is a separate process that dials INTO the app over WebSocket (the app
does NOT call out to the agent — the agent connects and the app pushes events
down to it).

### Slice A — users, RBAC, sessions
- `member` gained `password_hash` (PBKDF2, stdlib) + `role` (admin/member/agent).
- New `session` table; signed HMAC cookie `kb_session` (`security.py`).
- `SqliteAuthRepository`: members/RBAC/sessions; `auth` router
  (`/auth/register`, `/auth/login`, `/auth/logout`).
- `CurrentUserDep` resolves the logged-in member; board create/move/comment use
  it as the actor. Default login: **Bilal / admin123**.
- `CREWSPACE_SECRET` (signing), `CREWSPACE_REQUIRE_AUTH` (default False), `seed_admin_password`.

### Slice B — card audit fields
- `card` gained `created_by`/`updated_by`/`updated_at` (joined to member names).
- `add_card`/`move_card` set the actor; `CardDTO`/`mappers`/UI carry them.
- Card shows `· by <name>`; planner announcement includes `(by X)`.

### Slice C — bot/agent registration
- `member` gained `base_url` (informational: where the agent runs). Idempotent
  ALTER migration for existing DBs.
- `POST /auth/agents/register` (admin only) creates an `agent` member.
- Sidebar lists registered agents; "remote" tag when `base_url` set; "Register
  agent" link for admins.

### Slice D — pluggable / REMOTE agent runtime (Buzz-faithful)
- **Direction corrected**: agents are separate processes that connect to the app
  over WebSocket. The app pushes events DOWN to connected agents; agents act by
  sending frames back (chat `reply`, or `tool` calls that run the app's own
  tools — the MCP-equivalent seam). The app NEVER POSTs to the agent.
- `GET /agents/ws?token=<agent_token>` — agent dials in; token = HMAC(secret,
  agent_id) (stateless, no new column). `AgentConnectionManager` tracks
  `agent_id -> ws` and supports request/response correlation by `message_id`.
- `AgentRegistry` builds a local `StubAgent`/`LLMAgent` per registered member;
  `MultiAgentProvider` facade routes chat by `@mention` (WS if the agent is
  connected, else local in-process) and fans board events to every connected
  agent, each under its own identity/audit trail.
- `ChatService`/`BoardService` now build the facade from the uow at call time
  (no single hardcoded agent). `on_chat_message` returns `(agent_id, replies)`.
- `RemoteAgent` (old HTTP-POST dispatcher) DELETED — wrong direction.

### Verified
- 15 pytest pass (incl. LLM-agent mock through the service).
- Live: register agent "Coder" → it connects over `/agents/ws` → `@Coder ...`
  pushes a `chat` frame down its socket → agent replies → reply persisted as
  `agent_coder` and broadcast. Card create/move credit the real actor.
- WS handler now commits per message (agent-created cards durable immediately).

### Agent auth — Buzz-style Ed25519 (DONE)
- Each agent member owns an Ed25519 keypair. The PUBLIC key is stored in
  `member.pubkey`; the PRIVATE key is generated at registration and shown to the
  admin exactly once (must be copied into the agent process; never stored server-side).
- Connect: the agent sends `Authorization: Bearer <signed-claim>` (NOT a query-string
  token) where the claim `{agent_id, iat, nonce}` is Ed25519-signed. Server looks up
  the member, requires kind='agent', verifies the signature against `pubkey`, and
  rejects if `iat` is >60s old. This proves the agent POSSESSES its own key (not a
  shared secret), and is per-agent revocable/rotatable.
- Every action frame (reply/tool) carries a `sig` over its canonical JSON; the server
  re-verifies before applying — non-repudiable, signed-event audit (Buzz model).
- AUTHORIZATION (impersonation closed): the server FORCES the authenticated agent id as
  the actor on every tool call. `comment_card`/`post_message` `author_id` sent by the
  agent is ignored and replaced with the verified agent id. An agent can only ever
  author as itself.
- Transport: credential moved out of the URL (was `?token=`) into a header to avoid
  leaking in access logs. For production front with TLS (wss://) — reverse proxy
  (Caddy/nginx) or uvicorn ssl; over plain ws the bearer still travels in cleartext.
- Verified live: valid signed agent connects + runs tools; forged `author_id` is
  forced back to the agent; unsigned frame rejected; wrong-key frame rejected;
  no-auth connect rejected (4001).

### Agent LLM backend (DONE)
- `member.backend` column (`stub` | `llm`): each registered agent can be an LLM
  agent or a canned-stub agent, independently. `AgentRegistry._build_local_agent`
  picks `LLMAgent` vs `StubAgent` per member. The global `CREWSPACE_AGENT` flag still
  forces all-local to LLM as a shortcut.
- **LLM keys are NEVER stored in the DB.** In-app LLM agents read the server's
  `CREWSPACE_LLM_API_KEY` / `CREWSPACE_LLM_BASE_URL` env vars. Per-agent plaintext keys were
  explicitly rejected (MITM / DB-leak exposure). A *connected* (remote) agent runs
  its own LLM in its own process with its own key — the app never sees it (see
  docs/AGENT_PROTOCOL.md §7b). The LLM system prompt is now interpolated per-agent
  name (no longer hardcoded "Planner").
- The existing `LLMAgent` (OpenAI-compatible function calling, tools from the
  ToolRegistry, `client_factory` injected for tests) is reused unchanged.

## Suggested next steps (when resumed)
1. M0/M1/M2 + Slices A–D done. UI polished (sidebar, avatar, card ordering,
   move via dropdown + drag-and-drop, agent member list). Next roadmap items:
   M3 (multi-channel), M4 (postgres), and richer remote-agent tool use.
2. Resume the roadmap (PLAN.md): next is M3 (multi-channel) and M4 (postgres).
3. Add more unit tests for the tool handlers / services / remote-agent WS if
   desired (currently behavioral HTTP/WS + LLM-agent mock tests exist).

## Environment facts
- Python 3.14.7, `uv` 0.12.3. Project pinned `requires-python = ">=3.14"`.
- Deps: fastapi, uvicorn[standard], aiosqlite, jinja2, python-multipart, openai,
  pydantic-settings; dev: pytest, pytest-asyncio, httpx (via starlette TestClient),
  mcp (MCP server).
- Env config via pydantic-settings, `CREWSPACE_` prefix (CREWSPACE_DB_PATH, CREWSPACE_AGENT,
  CREWSPACE_LLM_API_KEY, CREWSPACE_LLM_BASE_URL, CREWSPACE_LLM_MODEL, CREWSPACE_HOST, CREWSPACE_PORT).
