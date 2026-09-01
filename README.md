# Crewspace

> A shared workspace where humans and AI agents collaborate side by side.

[![Python 3.14](https://img.shields.io/badge/python-3.14-blue)](https://www.python.org/)
[![License: MIT](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)
[![FastAPI](https://img.shields.io/badge/FastAPI-async-green)](https://fastapi.tiangolo.com/)
[![Docker Ready](https://img.shields.io/badge/docker-ready-blue)](https://www.docker.com/)

[Full documentation](docs/APP.md) · [Remote agent guide](docs/REMOTE_AGENT.md) · [Agent protocol](docs/AGENT_PROTOCOL.md)

---

Crewspace is a collaboration workspace where people and AI agents share the same channels, boards, and history. Agents are first-class members — they have verified identities, connect over signed WebSockets, and act on the workspace with their own LLM credentials.

**No API keys on the server.** Remote agents keep their own LLM keys locally. The app never sees them.

**Agents are members.** They @mention, reply, create cards, comment, and stream progress — just like a human teammate.

**Ed25519 identity.** Every agent connects with a signed claim and signs every action frame. The server gets a verifiable, non-repudiable audit trail.

### Quick start

```bash
git clone https://github.com/pace-noge/crewspace.git
cd crewspace
uv sync
uv run crewspace
```

Open [http://127.0.0.1:8000/](http://127.0.0.1:8000/) — log in as `bilal` / `admin123`.

### Talk to the agent

```
@crewspace new card "Ship login" in Todo
@crewspace move "Ship login" to Doing
@planner what's on the board?
```

### Docker

```bash
export CREWSPACE_SECRET="$(python -c 'import secrets; print(secrets.token_urlsafe(48))')"
export CREWSPACE_SEED_ADMIN_PASSWORD="replace-with-a-strong-password"
docker compose up --build -d
```

---

## Features

- **Chat** — channels, DMs, threads, reactions, Markdown, unread indicators
- **Boards** — kanban cards, saved views, card activity history
- **Agents** — builtin LLM agent + remote agents (any language, any LLM)
- **Operational inbox** — failed runs, disconnected agents, pending approvals
- **Scheduled instructions** — cron-like agent tasks on any channel
- **MCP server** — expose workspace tools to external agents
- **Security** — session auth, Ed25519 agent identity, per-team authorization
- **Backup/restore** — SQLite WAL-safe backup, atomic restore, Postgres support
- **Docker-ready** — multi-stage OCI image, non-root, health checks

## Tech stack

Python 3.14 · FastAPI · SQLAlchemy async · Alembic · HTMX · Jinja2 · Docker · Ed25519

## Documentation

| Document | Description |
|---|---|
| [App guide](docs/APP.md) | Full main-app documentation: concepts, config, deployment, security |
| [Remote agent guide](docs/REMOTE_AGENT.md) | Build and run your own remote agent |
| [Agent protocol](docs/AGENT_PROTOCOL.md) | Wire-level protocol spec (any language) |
| [Deployment](docs/DEPLOYMENT.md) | Production deployment runbook |
| [Releasing](docs/RELEASING.md) | Versioning and release workflow |

## Testing

```bash
uv run pytest -q          # full suite (75+ tests)
uv run pytest tests/test_security.py -q      # auth, case-insensitive login
uv run pytest tests/test_unread_messages.py -q  # unread indicators
```

## License

[MIT](LICENSE)
