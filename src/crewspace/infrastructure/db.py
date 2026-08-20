"""SQLAlchemy database lifecycle and Unit of Work.

Alembic owns schema evolution. The async engine supports SQLite via aiosqlite
and PostgreSQL via asyncpg; application and domain layers use repository/UoW
protocols and remain backend-neutral.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import logging
import shutil
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from sqlalchemy.pool import NullPool

from ..config import Settings
from ..domain.ports import UnitOfWork
from .lifecycle import SqlAlchemyLifecycleRepository
from .models import Base
from .repositories import (
    SqlAlchemyAgentPolicyRepository,
    SqlAlchemyAgentToolCallRepository,
    SqlAlchemyAuthRepository,
    SqlAlchemyBoardRepository,
    SqlAlchemyChannelRepository,
    SqlAlchemyChatRepository,
    SqlAlchemyScheduledJobRepository,
    SqlAlchemyTeamRepository,
    SqlAlchemyWorkspaceRepository,
    SqlAlchemyWorkflowRepository,
)
from .lifecycle import SqlAlchemyLifecycleRepository
from .sql import SqlAlchemyConnection


logger = logging.getLogger("crewspace.db")


class SqlAlchemyUnitOfWork:
    """A UnitOfWork over one SQLAlchemy async connection."""

    def __init__(self, conn: SqlAlchemyConnection) -> None:
        self._conn = conn
        self.pending_agent_tool_calls = []
        self.chat = SqlAlchemyChatRepository(conn)
        self.boards = SqlAlchemyBoardRepository(conn)
        self.auth = SqlAlchemyAuthRepository(conn)
        self.agent_policies = SqlAlchemyAgentPolicyRepository(conn)
        self.agent_tool_calls = SqlAlchemyAgentToolCallRepository(conn)
        self.teams = SqlAlchemyTeamRepository(conn)
        self.workspaces = SqlAlchemyWorkspaceRepository(conn)
        self.channels = SqlAlchemyChannelRepository(conn)
        self.lifecycle = SqlAlchemyLifecycleRepository(conn)
        self.scheduled_jobs = SqlAlchemyScheduledJobRepository(conn)
        self.workflows = SqlAlchemyWorkflowRepository(conn)

    def queue_agent_tool_call(self, call) -> None:
        self.pending_agent_tool_calls.append(call)

    async def commit(self) -> None:
        await self._conn.commit()

    async def rollback(self) -> None:
        await self._conn.rollback()

    async def close(self) -> None:
        pass


class Database:
    """Owns connection settings; mints a fresh UnitOfWork (own connection) per request."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        assert settings.database_url is not None
        kwargs: dict[str, Any] = {}
        # SQLite (the default local/dev backend) serializes writers on a single
        # file, so concurrent requests race for the write lock
        # ("database is locked"). A generous busy timeout lets writers queue
        # instead of failing. We also switch SQLite into WAL journal mode (see
        # Database.create) so a writer and readers can coexist. This branch is
        # skipped for PostgreSQL, which keeps its default pool and semantics.
        if settings.database_url.startswith("sqlite+"):
            kwargs["connect_args"] = {"timeout": 30}
        self.engine: AsyncEngine = create_async_engine(settings.database_url, **kwargs)

    @classmethod
    async def create(cls, settings: Settings) -> "Database":
        assert settings.database_url is not None
        is_sqlite = settings.database_url.startswith("sqlite+")
        if is_sqlite:
            cls._move_legacy_database(settings.db_path)
            await cls._normalize_legacy_sqlite(settings)
        await asyncio.to_thread(cls._upgrade_schema, settings.database_url)

        db = cls(settings)
        async with db.engine.connect() as raw:
            # SQLite: enable WAL so a writer and readers can run concurrently
            # and "database is locked" is far less likely under async load.
            # No-op for PostgreSQL (the dialect ignores the pragma).
            if settings.database_url.startswith("sqlite+"):
                await raw.exec_driver_sql("PRAGMA journal_mode=WAL")
            conn = SqlAlchemyConnection(raw)
            seeded = await _seed_if_needed(conn, settings.seed_admin_password)
            if seeded:
                await _ensure_seeded_agent_tool_permissions(conn)
            await raw.commit()
        return db

    @staticmethod
    def _upgrade_schema(database_url: str) -> str:
        root = Path(__file__).resolve().parents[3]
        config = Config(str(root / "alembic.ini"))
        config.set_main_option("script_location", str(root / "migrations"))
        config.set_main_option("sqlalchemy.url", database_url.replace("%", "%%"))
        # The application owns logging when Alembic is invoked programmatically;
        # direct ``alembic`` CLI invocations still use alembic.ini.
        config.attributes["configure_logger"] = False
        logger.info("Running database migrations (Alembic head)...")
        command.upgrade(config, "head")
        revision = ScriptDirectory.from_config(config).get_current_head() or "(unknown)"
        logger.info("Database migration finished; schema is at revision %s.", revision)
        return revision

    @classmethod
    async def _normalize_legacy_sqlite(cls, settings: Settings) -> None:
        target = Path(settings.db_path)
        if not target.exists():
            return
        assert settings.database_url is not None
        engine = create_async_engine(settings.database_url)
        try:
            async with engine.connect() as raw:
                conn = SqlAlchemyConnection(raw)
                tables = await (
                    await conn.execute(
                        "SELECT name FROM sqlite_master WHERE type='table' AND name='member'"
                    )
                ).fetchall()
                if tables:
                    await cls._migrate(conn)
                    await raw.commit()
        finally:
            await engine.dispose()

    @staticmethod
    def _move_legacy_database(db_path: str) -> None:
        """Move the pre-Crewspace database beside the configured default path."""
        target = Path(db_path)
        legacy = target.with_name("agentic-kanban.db")
        if target.name == "crewspace.db" and not target.exists() and legacy.exists():
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(legacy), str(target))

    @staticmethod
    async def _migrate(conn: SqlAlchemyConnection) -> None:
        """Idempotently add columns/tables for existing databases (no data loss)."""
        member_schema_row = await (
            await conn.execute(
                "SELECT sql FROM sqlite_master WHERE type='table' AND name='member'"
            )
        ).fetchone()
        member_schema = member_schema_row["sql"] if member_schema_row else ""
        if member_schema and "engineering_manager" not in member_schema:
            await conn.execute("PRAGMA foreign_keys = OFF")
            await conn.execute(
                """CREATE TABLE member_new (
                id TEXT PRIMARY KEY, kind TEXT NOT NULL CHECK (kind IN ('human','agent')),
                name TEXT NOT NULL, avatar TEXT, password_hash TEXT,
                role TEXT NOT NULL DEFAULT 'team_member'
                    CHECK (role IN ('superadmin','engineering_manager','team_member','agent')),
                base_url TEXT, pubkey TEXT,
                backend TEXT NOT NULL DEFAULT 'stub' CHECK (backend IN ('stub','llm')),
                uses_app_llm INTEGER NOT NULL DEFAULT 0,
                archived_at TEXT
                )"""
            )
            await conn.execute(
                """INSERT INTO member_new
                (id,kind,name,avatar,password_hash,role,base_url,pubkey,backend,uses_app_llm,archived_at)
                SELECT id,kind,name,avatar,password_hash,
                    CASE role WHEN 'admin' THEN 'superadmin'
                              WHEN 'member' THEN 'team_member' ELSE role END,
                    base_url,pubkey,backend,0,archived_at FROM member"""
            )
            await conn.execute("DROP TABLE member")
            await conn.execute("ALTER TABLE member_new RENAME TO member")
            await conn.execute("PRAGMA foreign_keys = ON")

        tables = {
            r["name"]
            for r in await (await conn.execute("SELECT name FROM sqlite_master WHERE type='table'")).fetchall()
        }
        if "member" not in tables:
            return

        async def cols_of(table: str) -> set[str]:
            return {
                r["name"]
                for r in await (await conn.execute(f"PRAGMA table_info({table})")).fetchall()
            }

        member_cols = await cols_of("member")
        if "password_hash" not in member_cols:
            await conn.execute("ALTER TABLE member ADD COLUMN password_hash TEXT")
        if "role" not in member_cols:
            await conn.execute("ALTER TABLE member ADD COLUMN role TEXT NOT NULL DEFAULT 'member'")
        if "base_url" not in member_cols:
            await conn.execute("ALTER TABLE member ADD COLUMN base_url TEXT")
        if "pubkey" not in member_cols:
            await conn.execute("ALTER TABLE member ADD COLUMN pubkey TEXT")
        if "backend" not in member_cols:
            await conn.execute("ALTER TABLE member ADD COLUMN backend TEXT NOT NULL DEFAULT 'stub'")
        if "archived_at" not in member_cols:
            await conn.execute("ALTER TABLE member ADD COLUMN archived_at TEXT")

        if "card" in tables:
            card_cols = await cols_of("card")
            for col in ("created_by", "updated_by", "updated_at"):
                if col not in card_cols:
                    await conn.execute(f"ALTER TABLE card ADD COLUMN {col} TEXT")
        if "message" in tables:
            msg_cols = await cols_of("message")
            if "thread_id" not in msg_cols:
                await conn.execute("ALTER TABLE message ADD COLUMN thread_id TEXT")
        if "scheduled_job" in tables:
            job_cols = await cols_of("scheduled_job")
            if "name" not in job_cols:
                await conn.execute("ALTER TABLE scheduled_job ADD COLUMN name TEXT NOT NULL DEFAULT 'Scheduled instruction'")
            if "description" not in job_cols:
                await conn.execute("ALTER TABLE scheduled_job ADD COLUMN description TEXT")
            if "claim_token" not in job_cols:
                await conn.execute("ALTER TABLE scheduled_job ADD COLUMN claim_token TEXT")
            if "claim_until" not in job_cols:
                await conn.execute("ALTER TABLE scheduled_job ADD COLUMN claim_until TEXT")

        if "workspace" in tables:
            ws_cols = await cols_of("workspace")
            if "team_id" not in ws_cols:
                await conn.execute("ALTER TABLE workspace ADD COLUMN team_id TEXT REFERENCES team(id)")
            if "created_by" not in ws_cols:
                await conn.execute("ALTER TABLE workspace ADD COLUMN created_by TEXT REFERENCES member(id)")
            if "created_at" not in ws_cols:
                await conn.execute("ALTER TABLE workspace ADD COLUMN created_at TEXT")
            if "archived_at" not in ws_cols:
                await conn.execute("ALTER TABLE workspace ADD COLUMN archived_at TEXT")

        if "channel" in tables:
            ch_cols = await cols_of("channel")
            if "channel_type" not in ch_cols:
                await conn.execute("ALTER TABLE channel ADD COLUMN channel_type TEXT NOT NULL DEFAULT 'permanent'")
            if "created_by" not in ch_cols:
                await conn.execute("ALTER TABLE channel ADD COLUMN created_by TEXT REFERENCES member(id)")
            if "created_at" not in ch_cols:
                await conn.execute("ALTER TABLE channel ADD COLUMN created_at TEXT")
            if "mention_policy" not in ch_cols:
                await conn.execute("ALTER TABLE channel ADD COLUMN mention_policy TEXT NOT NULL DEFAULT 'channel_members'")
            if "archived_at" not in ch_cols:
                await conn.execute("ALTER TABLE channel ADD COLUMN archived_at TEXT")

        if "team" in tables:
            team_cols = await cols_of("team")
            if "archived_at" not in team_cols:
                await conn.execute("ALTER TABLE team ADD COLUMN archived_at TEXT")

        if "channel_member" in tables:
            cm_cols = await cols_of("channel_member")
            if "role" not in cm_cols:
                await conn.execute("ALTER TABLE channel_member ADD COLUMN role TEXT NOT NULL DEFAULT 'member'")
            if "joined_at" not in cm_cols:
                await conn.execute("ALTER TABLE channel_member ADD COLUMN joined_at TEXT")
            if "invited_by" not in cm_cols:
                await conn.execute("ALTER TABLE channel_member ADD COLUMN invited_by TEXT REFERENCES member(id)")
            if "is_invitation_pending" not in cm_cols:
                await conn.execute("ALTER TABLE channel_member ADD COLUMN is_invitation_pending INTEGER NOT NULL DEFAULT 0")

        if "team" not in tables:
            await conn.executescript("""
                CREATE TABLE IF NOT EXISTS team (
                    id TEXT PRIMARY KEY, name TEXT NOT NULL,
                    created_by TEXT NOT NULL REFERENCES member(id),
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS team_member (
                    team_id TEXT NOT NULL REFERENCES team(id),
                    member_id TEXT NOT NULL REFERENCES member(id),
                    role TEXT NOT NULL DEFAULT 'member' CHECK (role IN ('leader','member')),
                    joined_at TEXT NOT NULL,
                    PRIMARY KEY (team_id, member_id)
                );
                CREATE TABLE IF NOT EXISTS workspace_member (
                    workspace_id TEXT NOT NULL REFERENCES workspace(id),
                    member_id TEXT NOT NULL REFERENCES member(id),
                    role TEXT NOT NULL DEFAULT 'member' CHECK (role IN ('admin','member')),
                    joined_at TEXT NOT NULL,
                    PRIMARY KEY (workspace_id, member_id)
                );
            """)

        if "session" not in tables:
            await conn.execute(
                "CREATE TABLE IF NOT EXISTS session ("
                "id TEXT PRIMARY KEY, member_id TEXT NOT NULL REFERENCES member(id), created_at TEXT NOT NULL)"
            )
        await conn.commit()

    @asynccontextmanager
    async def uow(self) -> AsyncIterator[UnitOfWork]:
        pending_tool_calls = []
        try:
            async with self.engine.connect() as raw:
                conn = SqlAlchemyConnection(raw)
                concrete_uow = SqlAlchemyUnitOfWork(conn)
                uow: UnitOfWork = concrete_uow
                try:
                    yield uow
                    await uow.commit()
                except Exception:
                    await uow.rollback()
                    raise
                finally:
                    pending_tool_calls = list(concrete_uow.pending_agent_tool_calls)
        finally:
            if pending_tool_calls:
                try:
                    async with self.engine.begin() as raw:
                        repository = SqlAlchemyAgentToolCallRepository(
                            SqlAlchemyConnection(raw)
                        )
                        for call in pending_tool_calls:
                            await repository.create(call)
                        await repository.prune()
                except Exception:
                    logger.exception("Failed to persist agent tool-call audit records")

    async def close(self) -> None:
        await self.engine.dispose()


async def _seed_if_needed(
    conn: SqlAlchemyConnection, admin_password: str = "admin123"
) -> bool:
    """Seed a new installation and report whether initial members were created."""
    team_row = await (await conn.execute("SELECT COUNT(*) AS n FROM team")).fetchone()
    member_row = await (await conn.execute("SELECT COUNT(*) AS n FROM member")).fetchone()
    if (team_row and team_row["n"] > 0) or (member_row and member_row["n"] > 0):
        await _ensure_builtin_assistant(conn)
        return False
    await _seed(conn, admin_password)
    return True


async def _ensure_seeded_agent_tool_permissions(conn: SqlAlchemyConnection) -> None:
    """Give pre-existing seeded builtin agents their legacy tool surface."""
    from ..application.tools import build_registry

    now = dt.datetime.now(dt.timezone.utc).isoformat()
    for agent_id in ("agent_crewspace", "agent_planner"):
        agent = await (await conn.execute(
            "SELECT id FROM member WHERE id=?", (agent_id,)
        )).fetchone()
        if not agent:
            continue
        for tool in build_registry().list_tools():
            existing = await (await conn.execute(
                "SELECT tool_name FROM agent_tool_permission WHERE agent_id=? "
                "AND provider_type='native' AND provider_id='crewspace' "
                "AND tool_name=?",
                (agent_id, tool.name),
            )).fetchone()
            if existing:
                continue
            await conn.execute(
                "INSERT INTO agent_tool_permission "
                "(agent_id, provider_type, provider_id, tool_name, enabled, "
                "approval_mode, created_at, updated_at) "
                "VALUES (?, 'native', 'crewspace', ?, 1, 'automatic', ?, ?)",
                (agent_id, tool.name, now, now),
            )


async def _ensure_builtin_assistant(conn: SqlAlchemyConnection) -> None:
    """Idempotently guarantee the non-deletable builtin assistant exists.

    Runs on every startup so the assistant can never be removed (even if someone
    deletes the row directly), and is re-added with the same fixed id.
    """
    from ..domain.identifiers import BUILTIN_ASSISTANT_ID

    existing = await (await conn.execute(
        "SELECT COUNT(*) AS n FROM member WHERE id = ? AND kind = 'agent'",
        (BUILTIN_ASSISTANT_ID,),
    )).fetchone()
    if existing and existing["n"] > 0:
        # Self-heal: ensure the builtin assistant is an app-LLM agent (a row may
        # have been seeded/imported with the wrong backend, which would route it
        # to the stub and produce canned replies instead of real LLM answers).
        await conn.execute(
            "UPDATE member SET backend = 'llm', uses_app_llm = 1 "
            "WHERE id = ? AND kind = 'agent'",
            (BUILTIN_ASSISTANT_ID,),
        )
        return

    now = dt.datetime.now(dt.timezone.utc).isoformat()
    await conn.execute(
        "INSERT INTO member (id, kind, name, avatar, password_hash, role, backend, uses_app_llm) "
        "VALUES (?, 'agent', 'Crewspace', '🛟', NULL, 'agent', 'llm', 1)",
        (BUILTIN_ASSISTANT_ID,),
    )
    # Re-attach to the seeded team/workspace/channel so it can see and act.
    team = await (await conn.execute("SELECT id FROM team LIMIT 1")).fetchone()
    ws = await (await conn.execute("SELECT id FROM workspace LIMIT 1")).fetchone()
    chan = await (await conn.execute("SELECT id FROM channel LIMIT 1")).fetchone()
    if team:
        await conn.execute(
            "INSERT OR IGNORE INTO team_member (team_id, member_id, role, joined_at) "
            "VALUES (?, ?, 'member', ?)",
            (team["id"], BUILTIN_ASSISTANT_ID, now),
        )
    if ws:
        await conn.execute(
            "INSERT OR IGNORE INTO workspace_member (workspace_id, member_id, role, joined_at) "
            "VALUES (?, ?, 'member', ?)",
            (ws["id"], BUILTIN_ASSISTANT_ID, now),
        )
    if chan:
        await conn.execute(
            "INSERT OR IGNORE INTO channel_member "
            "(channel_id, member_id, role, joined_at, invited_by, is_invitation_pending) "
            "VALUES (?, ?, 'member', ?, ?, 0)",
            (chan["id"], BUILTIN_ASSISTANT_ID, now, team["id"] if team else None),
        )


async def _seed(conn: SqlAlchemyConnection, admin_password: str = "admin123") -> None:
    import datetime as dt
    import uuid

    from ..security import hash_password

    now = dt.datetime.now(dt.timezone.utc).isoformat()
    uid = lambda: uuid.uuid4().hex

    # IDs for seed data - hardcoded to match test expectations
    team_id = "team_acme"
    ws_id = "ws_default"
    user_id = "user_bilal"
    agent_id = "agent_planner"
    builtin_id = "agent_crewspace"
    chan_id = "chan_general"
    board_id = "board_main"
    
    cols = [("col_todo", "To Do", 0), ("col_doing", "In Progress", 1), ("col_done", "Done", 2)]
    cards = [
        ("col_todo", "Draft launch announcement", 0),
        ("col_todo", "Design agent onboarding flow", 1),
        ("col_doing", "Wire websocket chat", 0),
        ("col_done", "Set up project skeleton", 0),
    ]

    # Members must precede creator-owned rows because PostgreSQL enforces FKs.
    await conn.executemany(
        "INSERT INTO member (id, kind, name, avatar, password_hash, role, backend, uses_app_llm) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        [
            (user_id, "human", "Bilal", "🧑", hash_password(admin_password), "superadmin", "stub", 0),
            (agent_id, "agent", "Planner", "🤖", None, "agent", "stub", 0),
            (builtin_id, "agent", "Crewspace", "🛟", None, "agent", "llm", 1),
        ],
    )
    await conn.execute(
        "INSERT INTO team (id, name, created_by, created_at) VALUES (?, ?, ?, ?)",
        (team_id, "Acme Corp", user_id, now),
    )
    await conn.execute(
        "INSERT INTO workspace (id, team_id, name, created_by, created_at) VALUES (?, ?, ?, ?, ?)",
        (ws_id, team_id, "Acme OS", user_id, now),
    )
    
    # Add members to team (user as leader)
    await conn.executemany(
        "INSERT INTO team_member (team_id, member_id, role, joined_at) VALUES (?, ?, ?, ?)",
        [
            (team_id, user_id, "leader", now),
            (team_id, agent_id, "member", now),
            (team_id, builtin_id, "member", now),
        ],
    )
    
    # Add members to workspace (user as admin)
    await conn.executemany(
        "INSERT INTO workspace_member (workspace_id, member_id, role, joined_at) VALUES (?, ?, ?, ?)",
        [
            (ws_id, user_id, "admin", now),
            (ws_id, agent_id, "member", now),
            (ws_id, builtin_id, "member", now),
        ],
    )
    
    # Create channel
    await conn.execute(
        "INSERT INTO channel (id, workspace_id, name, topic, channel_type, mention_policy, created_by, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (chan_id, ws_id, "general", "Team + agents", "permanent", "all_team", user_id, now),
    )
    
    # Add members to channel
    for mid in (user_id, agent_id, builtin_id):
        await conn.execute(
            "INSERT INTO channel_member (channel_id, member_id, role, joined_at, invited_by, is_invitation_pending) VALUES (?, ?, ?, ?, ?, ?)",
            (chan_id, mid, "member", now, user_id, 0),
        )
    
    # Create board
    await conn.execute(
        "INSERT INTO board (id, workspace_id, name) VALUES (?, ?, ?)", (board_id, ws_id, "Roadmap")
    )
    
    # Create board columns
    await conn.executemany(
        "INSERT INTO board_column (id, board_id, name, position) VALUES (?, ?, ?, ?)",
        [(c[0], board_id, c[1], c[2]) for c in cols],
    )
    
    # Create cards
    card_rows = [(uid(), c[0], c[1], c[2], user_id) for c in cards]
    await conn.executemany(
        "INSERT INTO card (id, column_id, title, position, created_by) VALUES (?, ?, ?, ?, ?)", card_rows
    )
    
    # Create initial message
    await conn.execute(
        "INSERT INTO message (id, channel_id, author_id, body, created_at) VALUES (?, ?, ?, ?, ?)",
        (
            uid(),
            chan_id,
            agent_id,
            "Hey! I'm Planner. Mention @planner to ask me to do something on the board.",
            now,
        ),
    )