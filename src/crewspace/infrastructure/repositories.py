"""Infrastructure: sqlite repository implementations.

Each method maps DB rows -> domain view entities. This is the single place
where SQL and the domain model meet. A Postgres port would reimplement these
against asyncpg while returning the exact same entities, so every layer above
(application services, API) is untouched.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import uuid

from sqlalchemy.exc import IntegrityError

from .sql import MappingRow, SqlAlchemyConnection

from ..domain.entities import (
    AgentToolCall,
    McpConnection,
    McpDiscoveredTool,
    BoardView,
    CardView,
    Channel,
    ChannelMembership,
    ChannelRole,
    ChannelType,
    ColumnView,
    CommentView,
    MemberKind,
    MessageView,
    Team,
    TeamMembership,
    TeamRole,
    ScheduleKind,
    ScheduledJob,
    ScheduledJobRun,
    Workspace,
    WorkspaceMembership,
    WorkspaceRole,
    Workflow,
    WorkflowRun,
    WorkflowRunStatus,
)
from ..domain.identifiers import DEFAULT_BOARD_ID, COLUMN_IDS
from ..domain.ports import (
    BoardRepository,
    ChannelRepository,
    ChatRepository,
    TeamRepository,
    WorkspaceRepository,
)


def _parse(ts: str) -> dt.datetime:
    return dt.datetime.fromisoformat(ts)


def _iso(value: dt.datetime | str) -> str:
    return value.isoformat() if isinstance(value, dt.datetime) else value


class SqlAlchemyChatRepository:
    def __init__(self, conn: SqlAlchemyConnection) -> None:
        self._conn = conn

    async def list_messages(self, channel_id: str, limit: int | None = None) -> list[MessageView]:
        if limit is None:
            cur = await self._conn.execute(
                """
                SELECT m.id, m.channel_id, m.author_id, m.body, m.created_at, m.thread_id,
                       mem.name AS author_name, mem.kind AS author_kind, mem.avatar
                FROM message m JOIN member mem ON mem.id = m.author_id
                WHERE m.channel_id = ? AND m.thread_id IS NULL ORDER BY m.created_at ASC
                """,
                (channel_id,),
            )
        else:
            cur = await self._conn.execute(
                """
                SELECT * FROM (
                    SELECT m.id, m.channel_id, m.author_id, m.body, m.created_at, m.thread_id,
                           mem.name AS author_name, mem.kind AS author_kind, mem.avatar
                    FROM message m JOIN member mem ON mem.id = m.author_id
                    WHERE m.channel_id = ? AND m.thread_id IS NULL ORDER BY m.created_at DESC LIMIT ?
                ) ORDER BY created_at ASC
                """,
                (channel_id, limit),
            )
        return [
            MessageView(
                id=r["id"],
                channel_id=r["channel_id"],
                author_id=r["author_id"],
                body=r["body"],
                created_at=_parse(r["created_at"]),
                thread_id=r["thread_id"],
                author_name=r["author_name"],
                author_kind=MemberKind(r["author_kind"]),
                author_avatar=r["avatar"],
            )
            for r in await cur.fetchall()
        ]

    async def list_thread(self, thread_id: str) -> list[MessageView]:
        cur = await self._conn.execute(
            """
            SELECT m.id, m.channel_id, m.author_id, m.body, m.created_at, m.thread_id,
                   mem.name AS author_name, mem.kind AS author_kind, mem.avatar
            FROM message m JOIN member mem ON mem.id = m.author_id
            WHERE m.id = ? OR m.thread_id = ? ORDER BY m.created_at ASC
            """,
            (thread_id, thread_id),
        )
        return [
            MessageView(
                id=r["id"],
                channel_id=r["channel_id"],
                author_id=r["author_id"],
                body=r["body"],
                created_at=_parse(r["created_at"]),
                thread_id=r["thread_id"],
                author_name=r["author_name"],
                author_kind=MemberKind(r["author_kind"]),
                author_avatar=r["avatar"],
            )
            for r in await cur.fetchall()
        ]

    async def thread_reply_count(self, thread_id: str) -> int:
        # Replies are messages whose thread_id == thread_id but id != thread_id.
        cur = await self._conn.execute(
            "SELECT COUNT(*) AS n FROM message WHERE thread_id = ? AND id != ?",
            (thread_id, thread_id),
        )
        row = await cur.fetchone()
        return int(row["n"]) if row else 0

    async def add_message(
        self, channel_id: str, author_id: str, body: str, thread_id: str | None = None
    ) -> MessageView:
        mid = uuid.uuid4().hex
        now = dt.datetime.now(dt.timezone.utc).isoformat()
        await self._conn.execute(
            "INSERT INTO message (id, channel_id, author_id, body, created_at, thread_id) VALUES (?, ?, ?, ?, ?, ?)",
            (mid, channel_id, author_id, body, now, thread_id),
        )
        # Enrich author name/avatar from the member table (history does the same
        # JOIN) so live-broadcast messages carry the avatar, not just stored ones.
        cur = await self._conn.execute(
            """
            SELECT m.id, m.channel_id, m.author_id, m.body, m.created_at, m.thread_id,
                   mem.name AS author_name, mem.kind AS author_kind, mem.avatar
            FROM message m JOIN member mem ON mem.id = m.author_id
            WHERE m.id = ?
            """,
            (mid,),
        )
        r = await cur.fetchone()
        return MessageView(
            id=r["id"],
            channel_id=r["channel_id"],
            author_id=r["author_id"],
            body=r["body"],
            created_at=_parse(r["created_at"]),
            thread_id=r["thread_id"],
            author_name=r["author_name"],
            author_kind=MemberKind(r["author_kind"]),
            author_avatar=r["avatar"],
        )

    async def list_reactions(self, message_id: str, member_id: str) -> list[dict]:
        cur = await self._conn.execute(
            """SELECT emoji, COUNT(*) AS count,
                      MAX(CASE WHEN member_id = ? THEN 1 ELSE 0 END) AS reacted
               FROM message_reaction WHERE message_id = ?
               GROUP BY emoji ORDER BY MIN(created_at), emoji""",
            (member_id, message_id),
        )
        return [
            {"emoji": row["emoji"], "count": int(row["count"]), "reacted": bool(row["reacted"])}
            for row in await cur.fetchall()
        ]

    async def toggle_reaction(self, message_id: str, member_id: str, emoji: str) -> list[dict]:
        cur = await self._conn.execute(
            "SELECT 1 FROM message_reaction WHERE message_id = ? AND member_id = ? AND emoji = ?",
            (message_id, member_id, emoji),
        )
        if await cur.fetchone():
            await self._conn.execute(
                "DELETE FROM message_reaction WHERE message_id = ? AND member_id = ? AND emoji = ?",
                (message_id, member_id, emoji),
            )
        else:
            await self._conn.execute(
                "INSERT INTO message_reaction (message_id, member_id, emoji, created_at) VALUES (?, ?, ?, ?)",
                (message_id, member_id, emoji, dt.datetime.now(dt.timezone.utc).isoformat()),
            )
        return await self.list_reactions(message_id, member_id)


class SqlAlchemyAuthRepository:
    """Members (humans + agents), RBAC roles, and sessions."""

    def __init__(self, conn: SqlAlchemyConnection) -> None:
        self._conn = conn

    @staticmethod
    def _member_columns() -> str:
        """Expose current role names while reading legacy databases."""
        return (
            "id, kind, name, avatar, password_hash, "
            "CASE role WHEN 'admin' THEN 'superadmin' "
            "WHEN 'member' THEN 'team_member' ELSE role END AS role, "
            "base_url, pubkey, backend, uses_app_llm"
        )

    async def _storage_role(self, role: str) -> str:
        """Roles are canonical after the legacy SQLite migration is applied."""
        return role

    async def get_member(self, member_id: str) -> MappingRow | None:
        cur = await self._conn.execute(
            f"SELECT {self._member_columns()} FROM member WHERE id = ? AND archived_at IS NULL", (member_id,)
        )
        return await cur.fetchone()

    async def get_member_by_name(self, name: str) -> MappingRow | None:
        cur = await self._conn.execute(
            f"SELECT {self._member_columns()} FROM member WHERE name = ? AND archived_at IS NULL", (name,)
        )
        return await cur.fetchone()

    async def create_member(
        self,
        member_id: str,
        kind: str,
        name: str,
        password: str | None,
        role: str,
        avatar: str | None = None,
        uses_app_llm: int = 0,
    ) -> None:
        from ..security import hash_password

        phash = hash_password(password) if password else None
        role = await self._storage_role(role)
        await self._conn.execute(
            "INSERT INTO member (id, kind, name, avatar, password_hash, role, uses_app_llm) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (member_id, kind, name, avatar, phash, role, uses_app_llm),
        )

    async def verify_password(self, member_id: str, password: str) -> bool:
        from ..security import verify_password

        row = await self.get_member(member_id)
        if not row or not row["password_hash"]:
            return False
        return verify_password(password, row["password_hash"])

    async def create_session(self, session_id: str, member_id: str) -> None:
        now = dt.datetime.now(dt.timezone.utc).isoformat()
        await self._conn.execute(
            "INSERT INTO session (id, member_id, created_at) VALUES (?, ?, ?)", (session_id, member_id, now)
        )

    async def get_session_member(self, session_id: str) -> dict | None:
        cur = await self._conn.execute(
            """
            SELECT m.id, m.kind, m.name, m.avatar,
                   CASE m.role WHEN 'admin' THEN 'superadmin'
                   WHEN 'member' THEN 'team_member' ELSE m.role END AS role
            FROM session s JOIN member m ON m.id = s.member_id WHERE s.id = ? AND m.archived_at IS NULL
            """,
            (session_id,),
        )
        return await cur.fetchone()

    async def delete_session(self, session_id: str) -> None:
        await self._conn.execute("DELETE FROM session WHERE id = ?", (session_id,))

    async def register_member(
        self, member_id: str, name: str, kind: str, avatar: str | None, role: str, base_url: str | None, pubkey: str | None = None, backend: str = "stub", uses_app_llm: int = 0
    ) -> None:
        await self._conn.execute(
            "INSERT INTO member (id, kind, name, avatar, role, base_url, pubkey, backend, uses_app_llm) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (member_id, kind, name, avatar, role, base_url, pubkey, backend, uses_app_llm),
        )

    async def get_pubkey(self, member_id: str) -> str | None:
        cur = await self._conn.execute("SELECT pubkey FROM member WHERE id = ? AND archived_at IS NULL", (member_id,))
        row = await cur.fetchone()
        return row["pubkey"] if row else None

    async def list_members(self, kind: str | None = None) -> list[MappingRow]:
        cols = (
            "id, kind, name, avatar, "
            "CASE role WHEN 'admin' THEN 'superadmin' "
            "WHEN 'member' THEN 'team_member' ELSE role END AS role, "
            "base_url, pubkey, backend, uses_app_llm"
        )
        if kind:
            cur = await self._conn.execute(
                f"SELECT {cols} FROM member WHERE kind = ? AND archived_at IS NULL ORDER BY name", (kind,)
            )
        else:
            cur = await self._conn.execute(
                f"SELECT {cols} FROM member WHERE archived_at IS NULL ORDER BY name"
            )
        return list(await cur.fetchall())


class SqlAlchemyAgentPolicyRepository:
    def __init__(self, conn: SqlAlchemyConnection) -> None:
        self._conn = conn

    async def list_enabled_native_tools(self, agent_id: str) -> set[str]:
        cur = await self._conn.execute(
            "SELECT tool_name FROM agent_tool_permission "
            "WHERE agent_id=? AND provider_type='native' "
            "AND provider_id='crewspace' AND enabled=1",
            (agent_id,),
        )
        return {row["tool_name"] for row in await cur.fetchall()}

    async def replace_native_tools(self, agent_id: str, tool_names: set[str]) -> None:
        await self._conn.execute(
            "DELETE FROM agent_tool_permission WHERE agent_id=? "
            "AND provider_type='native' AND provider_id='crewspace'",
            (agent_id,),
        )
        now = dt.datetime.now(dt.timezone.utc).isoformat()
        for tool_name in sorted(tool_names):
            await self._conn.execute(
                "INSERT INTO agent_tool_permission "
                "(agent_id, provider_type, provider_id, tool_name, enabled, "
                "approval_mode, created_at, updated_at) "
                "VALUES (?, 'native', 'crewspace', ?, 1, 'automatic', ?, ?)",
                (agent_id, tool_name, now, now),
            )

    async def list_enabled_mcp_tools(self, agent_id: str) -> set[tuple[str, str]]:
        cur = await self._conn.execute(
            "SELECT provider_id, tool_name FROM agent_tool_permission "
            "WHERE agent_id=? AND provider_type='mcp' AND enabled=1",
            (agent_id,),
        )
        return {
            (row["provider_id"], row["tool_name"])
            for row in await cur.fetchall()
        }

    async def replace_mcp_tools(
        self, agent_id: str, tools: set[tuple[str, str]],
    ) -> None:
        await self._conn.execute(
            "DELETE FROM agent_tool_permission WHERE agent_id=? "
            "AND provider_type='mcp'",
            (agent_id,),
        )
        now = dt.datetime.now(dt.timezone.utc).isoformat()
        for provider_id, tool_name in sorted(tools):
            await self._conn.execute(
                "INSERT INTO agent_tool_permission "
                "(agent_id, provider_type, provider_id, tool_name, enabled, "
                "approval_mode, created_at, updated_at) "
                "VALUES (?, 'mcp', ?, ?, 1, 'automatic', ?, ?)",
                (agent_id, provider_id, tool_name, now, now),
            )


class SqlAlchemyAgentToolCallRepository:
    def __init__(self, conn: SqlAlchemyConnection) -> None:
        self._conn = conn

    @staticmethod
    def _map(row) -> AgentToolCall:
        return AgentToolCall(
            id=row["id"], agent_id=row["agent_id"],
            initiator_id=row["initiator_id"], provider_type=row["provider_type"],
            provider_id=row["provider_id"], tool_name=row["tool_name"],
            status=row["status"], arguments_redacted=row["arguments_redacted"],
            result_summary=row["result_summary"], error=row["error"],
            duration_ms=row["duration_ms"], created_at=_parse(row["created_at"]),
        )

    async def create(self, call: AgentToolCall) -> AgentToolCall:
        await self._conn.execute(
            "INSERT INTO agent_tool_call "
            "(id, agent_id, initiator_id, provider_type, provider_id, tool_name, "
            "status, arguments_redacted, result_summary, error, duration_ms, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (call.id, call.agent_id, call.initiator_id, call.provider_type,
             call.provider_id, call.tool_name, call.status,
             call.arguments_redacted, call.result_summary, call.error,
             call.duration_ms, _iso(call.created_at)),
        )
        return call

    async def finish(
        self, call_id: str, *, status: str, duration_ms: int,
        result_summary: str | None, error: str | None,
    ) -> None:
        await self._conn.execute(
            "UPDATE agent_tool_call SET status=?, duration_ms=?, "
            "result_summary=?, error=? WHERE id=?",
            (status, duration_ms, result_summary, error, call_id),
        )

    async def list_recent(self, limit: int = 100) -> list[AgentToolCall]:
        cur = await self._conn.execute(
            "SELECT * FROM agent_tool_call ORDER BY created_at DESC LIMIT ?",
            (limit,),
        )
        return [self._map(row) for row in await cur.fetchall()]

    async def prune(self, keep: int = 10_000) -> None:
        if keep < 1:
            raise ValueError("Audit retention must keep at least one row")
        await self._conn.execute(
            "DELETE FROM agent_tool_call WHERE id NOT IN ("
            "SELECT id FROM agent_tool_call "
            "ORDER BY created_at DESC, id DESC LIMIT ?)",
            (keep,),
        )


class SqlAlchemyMcpConnectionRepository:
    def __init__(self, conn: SqlAlchemyConnection) -> None:
        self._conn = conn

    @staticmethod
    def _map_connection(row) -> McpConnection:
        return McpConnection(
            id=row["id"], name=row["name"], namespace=row["namespace"],
            transport=row["transport"],
            endpoint_or_command=row["endpoint_or_command"],
            enabled=bool(row["enabled"]),
            auth_secret_ref=row["auth_secret_ref"], created_by=row["created_by"],
            created_at=_parse(row["created_at"]), updated_at=_parse(row["updated_at"]),
        )

    @staticmethod
    def _map_tool(row) -> McpDiscoveredTool:
        return McpDiscoveredTool(
            connection_id=row["connection_id"], tool_name=row["tool_name"],
            description=row["description"], input_schema=json.loads(row["input_schema"]),
            schema_hash=row["schema_hash"], approval_state=row["approval_state"],
            discovered_at=_parse(row["discovered_at"]),
        )

    async def create(self, connection: McpConnection) -> McpConnection:
        try:
            await self._conn.execute(
                "INSERT INTO mcp_connection "
                "(id, name, namespace, transport, endpoint_or_command, enabled, "
                "auth_secret_ref, created_by, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (connection.id, connection.name, connection.namespace,
                 connection.transport, connection.endpoint_or_command,
                 int(connection.enabled), connection.auth_secret_ref,
                 connection.created_by, _iso(connection.created_at),
                 _iso(connection.updated_at)),
            )
        except IntegrityError as exc:
            constraint_name = getattr(getattr(exc.orig, "diag", None), "constraint_name", None)
            sqlite_namespace_conflict = (
                "UNIQUE constraint failed: mcp_connection.namespace" in str(exc.orig)
            )
            if constraint_name == "uq_mcp_connection_namespace" or sqlite_namespace_conflict:
                raise ValueError("MCP namespace is already in use") from exc
            raise
        return connection

    async def get(self, connection_id: str) -> McpConnection | None:
        row = await (await self._conn.execute(
            "SELECT * FROM mcp_connection WHERE id=?", (connection_id,)
        )).fetchone()
        return self._map_connection(row) if row else None

    async def get_by_namespace(self, namespace: str) -> McpConnection | None:
        row = await (await self._conn.execute(
            "SELECT * FROM mcp_connection WHERE namespace=?", (namespace,)
        )).fetchone()
        return self._map_connection(row) if row else None

    async def list_connections(self) -> list[McpConnection]:
        rows = await (await self._conn.execute(
            "SELECT * FROM mcp_connection ORDER BY name, id"
        )).fetchall()
        return [self._map_connection(row) for row in rows]

    async def set_enabled(self, connection_id: str, enabled: bool) -> None:
        result = await self._conn.execute(
            "UPDATE mcp_connection SET enabled=?, updated_at=? WHERE id=?",
            (int(enabled), _iso(dt.datetime.now(dt.timezone.utc)), connection_id),
        )
        if result.rowcount == 0:
            raise KeyError(f"Unknown MCP connection {connection_id!r}")

    async def upsert_discovered_tool(self, tool: McpDiscoveredTool) -> None:
        schema = json.dumps(tool.input_schema, sort_keys=True, separators=(",", ":"))
        fingerprint_payload = json.dumps(
            {
                "name": tool.tool_name,
                "description": tool.description,
                "input_schema": tool.input_schema,
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
        schema_hash = f"sha256:{hashlib.sha256(fingerprint_payload).hexdigest()}"
        existing = await (await self._conn.execute(
            "SELECT schema_hash, approval_state FROM mcp_discovered_tool "
            "WHERE connection_id=? AND tool_name=?",
            (tool.connection_id, tool.tool_name),
        )).fetchone()
        approval_state = tool.approval_state
        if existing:
            approval_state = (
                existing["approval_state"]
                if existing["schema_hash"] == schema_hash
                else "changed"
            )
        else:
            approval_state = "pending"
        result = await self._conn.execute(
            "UPDATE mcp_discovered_tool SET description=?, input_schema=?, "
            "schema_hash=?, approval_state=?, discovered_at=? "
            "WHERE connection_id=? AND tool_name=?",
            (tool.description, schema, schema_hash, approval_state,
             _iso(tool.discovered_at), tool.connection_id, tool.tool_name),
        )
        if result.rowcount == 0:
            await self._conn.execute(
                "INSERT INTO mcp_discovered_tool "
                "(connection_id, tool_name, description, input_schema, schema_hash, "
                "approval_state, discovered_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (tool.connection_id, tool.tool_name, tool.description, schema,
                 schema_hash, approval_state, _iso(tool.discovered_at)),
            )

    async def set_tool_approval_state(
        self, connection_id: str, tool_name: str, state: str,
    ) -> None:
        if state not in {"pending", "approved", "changed", "disabled"}:
            raise ValueError(f"Invalid MCP tool approval state {state!r}")
        result = await self._conn.execute(
            "UPDATE mcp_discovered_tool SET approval_state=? "
            "WHERE connection_id=? AND tool_name=?",
            (state, connection_id, tool_name),
        )
        if result.rowcount == 0:
            raise KeyError(f"Unknown MCP tool {connection_id}.{tool_name}")

    async def disable_missing_tools(
        self, connection_id: str, present_names: set[str],
    ) -> None:
        rows = await (await self._conn.execute(
            "SELECT tool_name FROM mcp_discovered_tool WHERE connection_id=?",
            (connection_id,),
        )).fetchall()
        for row in rows:
            if row["tool_name"] not in present_names:
                await self._conn.execute(
                    "UPDATE mcp_discovered_tool SET approval_state='disabled' "
                    "WHERE connection_id=? AND tool_name=?",
                    (connection_id, row["tool_name"]),
                )

    async def list_discovered_tools(
        self, connection_id: str,
    ) -> list[McpDiscoveredTool]:
        rows = await (await self._conn.execute(
            "SELECT * FROM mcp_discovered_tool WHERE connection_id=? "
            "ORDER BY tool_name", (connection_id,)
        )).fetchall()
        return [self._map_tool(row) for row in rows]

    async def get_discovered_tool(
        self, connection_id: str, tool_name: str,
    ) -> McpDiscoveredTool | None:
        row = await (await self._conn.execute(
            "SELECT * FROM mcp_discovered_tool "
            "WHERE connection_id=? AND tool_name=?",
            (connection_id, tool_name),
        )).fetchone()
        return self._map_tool(row) if row else None


class SqlAlchemyScheduledJobRepository:
    def __init__(self, conn: SqlAlchemyConnection) -> None:
        self._conn = conn

    @staticmethod
    def _to_job(row) -> ScheduledJob:
        return ScheduledJob(
            id=row["id"], name=row["name"], description=row["description"],
            channel_id=row["channel_id"], instruction=row["instruction"],
            schedule_kind=ScheduleKind(row["schedule_kind"]), creator_id=row["creator_id"],
            interval_value=row["interval_value"], interval_unit=row["interval_unit"],
            daily_time=row["daily_time"], enabled=bool(row["enabled"]),
            next_run_at=_parse(row["next_run_at"]), created_at=_parse(row["created_at"]),
            last_run_at=_parse(row["last_run_at"]) if row["last_run_at"] else None,
            last_status=row["last_status"], last_error=row["last_error"],
        )

    async def create(self, job: ScheduledJob) -> ScheduledJob:
        await self._conn.execute(
            """INSERT INTO scheduled_job
            (id,name,description,channel_id,instruction,schedule_kind,interval_value,interval_unit,daily_time,
             creator_id,enabled,next_run_at,created_at,last_run_at,last_status,last_error)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (job.id, job.name, job.description, job.channel_id, job.instruction, job.schedule_kind.value,
             job.interval_value, job.interval_unit, job.daily_time, job.creator_id,
             int(job.enabled), job.next_run_at.isoformat(),
             (job.created_at or dt.datetime.now(dt.timezone.utc)).isoformat(),
             None, None, None),
        )
        return job

    async def get(self, job_id: str) -> ScheduledJob | None:
        row = await (await self._conn.execute(
            "SELECT * FROM scheduled_job WHERE id = ?", (job_id,)
        )).fetchone()
        return self._to_job(row) if row else None

    async def update(self, job: ScheduledJob) -> ScheduledJob:
        await self._conn.execute(
            """UPDATE scheduled_job SET name=?, description=?, channel_id=?, instruction=?,
            schedule_kind=?, interval_value=?, interval_unit=?, daily_time=?, next_run_at=?
            WHERE id=?""",
            (
                job.name, job.description, job.channel_id, job.instruction,
                job.schedule_kind.value, job.interval_value, job.interval_unit,
                job.daily_time, job.next_run_at.isoformat(), job.id,
            ),
        )
        return job

    async def set_enabled(
        self, job_id: str, *, enabled: bool, next_run_at: dt.datetime | None = None
    ) -> None:
        if next_run_at is None:
            await self._conn.execute(
                "UPDATE scheduled_job SET enabled=? WHERE id=?", (int(enabled), job_id)
            )
            return
        await self._conn.execute(
            "UPDATE scheduled_job SET enabled=?, next_run_at=? WHERE id=?",
            (int(enabled), next_run_at.isoformat(), job_id),
        )

    async def delete(self, job_id: str) -> None:
        await self._conn.execute("DELETE FROM scheduled_job_run WHERE job_id=?", (job_id,))
        await self._conn.execute("DELETE FROM scheduled_job WHERE id=?", (job_id,))

    async def list_for_channels(self, channel_ids: list[str]) -> list[ScheduledJob]:
        if not channel_ids:
            return []
        marks = ",".join("?" for _ in channel_ids)
        rows = await (await self._conn.execute(
            f"SELECT * FROM scheduled_job WHERE channel_id IN ({marks}) ORDER BY created_at DESC",
            channel_ids,
        )).fetchall()
        return [self._to_job(row) for row in rows]

    async def list_due(self, now: dt.datetime) -> list[ScheduledJob]:
        rows = await (await self._conn.execute(
            """SELECT j.* FROM scheduled_job j
               JOIN channel c ON c.id=j.channel_id
               JOIN workspace w ON w.id=c.workspace_id
               JOIN team t ON t.id=w.team_id
               WHERE j.enabled=1 AND j.next_run_at<=?
               AND c.archived_at IS NULL AND w.archived_at IS NULL
               AND t.archived_at IS NULL ORDER BY j.next_run_at""",
            (now.isoformat(),),
        )).fetchall()
        return [self._to_job(row) for row in rows]

    async def claim_due(
        self, now: dt.datetime, *, claim_token: str, claim_until: dt.datetime
    ) -> list[ScheduledJob]:
        """Atomically lease all currently due jobs to one scheduler worker."""
        rows = await (await self._conn.execute(
            """UPDATE scheduled_job
               SET claim_token=?, claim_until=?
               WHERE id IN (
                   SELECT j.id FROM scheduled_job j
                   JOIN channel c ON c.id=j.channel_id
                   JOIN workspace w ON w.id=c.workspace_id
                   JOIN team t ON t.id=w.team_id
                   WHERE j.enabled=1 AND j.next_run_at<=?
                   AND (j.claim_until IS NULL OR j.claim_until<=?)
                   AND c.archived_at IS NULL AND w.archived_at IS NULL
                   AND t.archived_at IS NULL
               )
               RETURNING *""",
            (
                claim_token,
                claim_until.isoformat(),
                now.isoformat(),
                now.isoformat(),
            ),
        )).fetchall()
        return [self._to_job(row) for row in rows]

    async def record_run(self, job_id: str, *, next_run_at, enabled: bool,
                         status: str, error: str | None, run_at) -> None:
        await self._conn.execute(
            """UPDATE scheduled_job SET next_run_at=?, enabled=?, last_run_at=?,
            last_status=?, last_error=?, claim_token=NULL, claim_until=NULL WHERE id=?""",
            (next_run_at.isoformat(), int(enabled), run_at.isoformat(), status, error, job_id),
        )

    @staticmethod
    def _to_run(row) -> ScheduledJobRun:
        return ScheduledJobRun(
            id=row["id"], job_id=row["job_id"], trigger=row["trigger"],
            initiated_by=row["initiated_by"], instruction=row["instruction"],
            channel_id=row["channel_id"], scheduled_for=_parse(row["scheduled_for"]),
            started_at=_parse(row["started_at"]),
            finished_at=_parse(row["finished_at"]) if row["finished_at"] else None,
            duration_ms=row["duration_ms"], status=row["status"],
            message_ids=json.loads(row["message_ids"] or "[]"), error=row["error"],
            next_run_at=_parse(row["next_run_at"]) if row["next_run_at"] else None,
        )

    async def start_run(self, run: ScheduledJobRun) -> ScheduledJobRun:
        await self._conn.execute(
            """INSERT INTO scheduled_job_run
            (id,job_id,trigger,initiated_by,instruction,channel_id,scheduled_for,
             started_at,status,message_ids) VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (run.id, run.job_id, run.trigger, run.initiated_by, run.instruction,
             run.channel_id, run.scheduled_for.isoformat(), run.started_at.isoformat(),
             run.status, json.dumps(run.message_ids)),
        )
        return run

    async def finish_run(self, run_id: str, *, status: str, finished_at,
                         duration_ms: int, message_ids: list[str], error: str | None,
                         next_run_at) -> None:
        await self._conn.execute(
            """UPDATE scheduled_job_run SET status=?, finished_at=?, duration_ms=?,
            message_ids=?, error=?, next_run_at=? WHERE id=?""",
            (status, finished_at.isoformat(), duration_ms, json.dumps(message_ids), error,
             next_run_at.isoformat() if next_run_at else None, run_id),
        )

    async def list_runs(self, job_id: str, limit: int = 100) -> list[ScheduledJobRun]:
        rows = await (await self._conn.execute(
            "SELECT * FROM scheduled_job_run WHERE job_id=? ORDER BY started_at DESC LIMIT ?",
            (job_id, limit),
        )).fetchall()
        return [self._to_run(row) for row in rows]

    async def get_run(self, run_id: str) -> ScheduledJobRun | None:
        row = await (await self._conn.execute(
            "SELECT * FROM scheduled_job_run WHERE id=?", (run_id,)
        )).fetchone()
        return self._to_run(row) if row else None


class SqlAlchemyTeamRepository:
    def __init__(self, conn: SqlAlchemyConnection) -> None:
        self._conn = conn

    async def create_team(self, team: Team) -> Team:
        await self._conn.execute(
            "INSERT INTO team (id, name, created_by, created_at) VALUES (?, ?, ?, ?)",
            (team.id, team.name, team.created_by, team.created_at.isoformat()),
        )
        return team

    async def get_team(self, team_id: str) -> Team | None:
        cur = await self._conn.execute("SELECT id, name, created_by, created_at FROM team WHERE id = ? AND archived_at IS NULL", (team_id,))
        row = await cur.fetchone()
        if not row:
            return None
        return Team(
            id=row["id"],
            name=row["name"],
            created_by=row["created_by"],
            created_at=_parse(row["created_at"]),
        )

    async def list_teams(self) -> list[Team]:
        cur = await self._conn.execute(
            "SELECT id, name, created_by, created_at FROM team WHERE archived_at IS NULL ORDER BY name"
        )
        return [
            Team(id=r["id"], name=r["name"], created_by=r["created_by"],
                 created_at=_parse(r["created_at"]))
            for r in await cur.fetchall()
        ]

    async def list_teams_for_member(self, member_id: str) -> list[Team]:
        cur = await self._conn.execute(
            """
            SELECT t.id, t.name, t.created_by, t.created_at
            FROM team t
            JOIN team_member tm ON tm.team_id = t.id
            WHERE tm.member_id = ? AND t.archived_at IS NULL
            """,
            (member_id,),
        )
        return [
            Team(
                id=r["id"],
                name=r["name"],
                created_by=r["created_by"],
                created_at=_parse(r["created_at"]),
            )
            for r in await cur.fetchall()
        ]

    async def add_member(self, membership: TeamMembership) -> None:
        now = dt.datetime.now(dt.timezone.utc).isoformat()
        await self._conn.execute(
            "INSERT INTO team_member (team_id, member_id, role, joined_at) VALUES (?, ?, ?, ?)",
            (
                membership.team_id,
                membership.member_id,
                membership.role.value,
                _iso(membership.joined_at) if membership.joined_at else now,
            ),
        )

    async def remove_member(self, team_id: str, member_id: str) -> None:
        await self._conn.execute(
            "DELETE FROM team_member WHERE team_id = ? AND member_id = ?", (team_id, member_id)
        )

    async def get_membership(self, team_id: str, member_id: str) -> TeamMembership | None:
        cur = await self._conn.execute(
            "SELECT team_id, member_id, role, joined_at FROM team_member WHERE team_id = ? AND member_id = ?",
            (team_id, member_id),
        )
        row = await cur.fetchone()
        if not row:
            return None
        return TeamMembership(
            team_id=row["team_id"],
            member_id=row["member_id"],
            role=TeamRole(row["role"]),
            joined_at=_parse(row["joined_at"]),
        )

    async def list_members(self, team_id: str) -> list[TeamMembership]:
        cur = await self._conn.execute(
            "SELECT team_id, member_id, role, joined_at FROM team_member WHERE team_id = ?", (team_id,)
        )
        return [
            TeamMembership(
                team_id=r["team_id"],
                member_id=r["member_id"],
                role=TeamRole(r["role"]),
                joined_at=_parse(r["joined_at"]),
            )
            for r in await cur.fetchall()
        ]

    async def is_leader(self, team_id: str, member_id: str) -> bool:
        cur = await self._conn.execute(
            "SELECT role FROM team_member WHERE team_id = ? AND member_id = ?", (team_id, member_id)
        )
        row = await cur.fetchone()
        return row is not None and row["role"] == "leader"


class SqlAlchemyWorkspaceRepository:
    def __init__(self, conn: SqlAlchemyConnection) -> None:
        self._conn = conn

    async def create_workspace(self, workspace: Workspace) -> Workspace:
        await self._conn.execute(
            "INSERT INTO workspace (id, team_id, name, created_by, created_at) VALUES (?, ?, ?, ?, ?)",
            (workspace.id, workspace.team_id, workspace.name, workspace.created_by, workspace.created_at.isoformat()),
        )
        return workspace

    async def get_workspace(self, workspace_id: str) -> Workspace | None:
        cur = await self._conn.execute(
            "SELECT id, team_id, name, created_by, created_at FROM workspace WHERE id = ? AND archived_at IS NULL", (workspace_id,)
        )
        row = await cur.fetchone()
        if not row:
            return None
        return Workspace(
            id=row["id"],
            team_id=row["team_id"],
            name=row["name"],
            created_by=row["created_by"],
            created_at=_parse(row["created_at"]),
        )

    async def update_name(self, workspace_id: str, name: str) -> None:
        await self._conn.execute(
            "UPDATE workspace SET name = ? WHERE id = ?", (name, workspace_id)
        )

    async def list_workspaces_for_team(self, team_id: str) -> list[Workspace]:
        cur = await self._conn.execute(
            "SELECT id, team_id, name, created_by, created_at FROM workspace WHERE team_id = ? AND archived_at IS NULL", (team_id,)
        )
        return [
            Workspace(
                id=r["id"],
                team_id=r["team_id"],
                name=r["name"],
                created_by=r["created_by"],
                created_at=_parse(r["created_at"]),
            )
            for r in await cur.fetchall()
        ]

    async def list_workspaces_for_member(self, member_id: str) -> list[Workspace]:
        cur = await self._conn.execute(
            """
            SELECT w.id, w.team_id, w.name, w.created_by, w.created_at
            FROM workspace w
            JOIN workspace_member wm ON wm.workspace_id = w.id
            WHERE wm.member_id = ? AND w.archived_at IS NULL
            """,
            (member_id,),
        )
        return [
            Workspace(
                id=r["id"],
                team_id=r["team_id"],
                name=r["name"],
                created_by=r["created_by"],
                created_at=_parse(r["created_at"]),
            )
            for r in await cur.fetchall()
        ]

    async def add_member(self, membership: WorkspaceMembership) -> None:
        now = dt.datetime.now(dt.timezone.utc).isoformat()
        await self._conn.execute(
            "INSERT INTO workspace_member (workspace_id, member_id, role, joined_at) VALUES (?, ?, ?, ?)",
            (
                membership.workspace_id,
                membership.member_id,
                membership.role.value,
                _iso(membership.joined_at) if membership.joined_at else now,
            ),
        )

    async def remove_member(self, workspace_id: str, member_id: str) -> None:
        await self._conn.execute(
            "DELETE FROM workspace_member WHERE workspace_id = ? AND member_id = ?", (workspace_id, member_id)
        )

    async def get_membership(self, workspace_id: str, member_id: str) -> WorkspaceMembership | None:
        cur = await self._conn.execute(
            "SELECT workspace_id, member_id, role, joined_at FROM workspace_member WHERE workspace_id = ? AND member_id = ?",
            (workspace_id, member_id),
        )
        row = await cur.fetchone()
        if not row:
            return None
        return WorkspaceMembership(
            workspace_id=row["workspace_id"],
            member_id=row["member_id"],
            role=WorkspaceRole(row["role"]),
            joined_at=_parse(row["joined_at"]),
        )

    async def list_members(self, workspace_id: str) -> list[WorkspaceMembership]:
        cur = await self._conn.execute(
            "SELECT workspace_id, member_id, role, joined_at FROM workspace_member WHERE workspace_id = ?", (workspace_id,)
        )
        return [
            WorkspaceMembership(
                workspace_id=r["workspace_id"],
                member_id=r["member_id"],
                role=WorkspaceRole(r["role"]),
                joined_at=_parse(r["joined_at"]),
            )
            for r in await cur.fetchall()
        ]

    async def is_admin(self, workspace_id: str, member_id: str) -> bool:
        cur = await self._conn.execute(
            "SELECT role FROM workspace_member WHERE workspace_id = ? AND member_id = ?", (workspace_id, member_id)
        )
        row = await cur.fetchone()
        return row is not None and row["role"] == "admin"


class SqlAlchemyChannelRepository:
    def __init__(self, conn: SqlAlchemyConnection) -> None:
        self._conn = conn

    async def create_channel(self, channel: Channel) -> Channel:
        await self._conn.execute(
            """INSERT INTO channel (id, workspace_id, name, topic, channel_type, mention_policy, created_by, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (channel.id, channel.workspace_id, channel.name, channel.topic, channel.channel_type.value, channel.mention_policy, channel.created_by, channel.created_at.isoformat() if channel.created_at else dt.datetime.now(dt.timezone.utc).isoformat()),
        )
        return channel

    async def get_channel(self, channel_id: str) -> Channel | None:
        cur = await self._conn.execute(
            "SELECT id, workspace_id, name, topic, channel_type, mention_policy, created_by, created_at FROM channel WHERE id = ? AND archived_at IS NULL", (channel_id,)
        )
        row = await cur.fetchone()
        if not row:
            return None
        return Channel(
            id=row["id"],
            workspace_id=row["workspace_id"],
            name=row["name"],
            topic=row["topic"],
            channel_type=ChannelType(row["channel_type"]),
            mention_policy=row["mention_policy"],
            created_by=row["created_by"],
            created_at=_parse(row["created_at"]),
        )

    async def update_channel(self, channel: Channel) -> None:
        await self._conn.execute(
            """UPDATE channel
            SET name = ?, topic = ?, channel_type = ?, mention_policy = ?
            WHERE id = ?""",
            (
                channel.name,
                channel.topic,
                channel.channel_type.value,
                channel.mention_policy,
                channel.id,
            ),
        )

    async def list_channels_for_workspace(self, workspace_id: str) -> list[Channel]:
        cur = await self._conn.execute(
            """SELECT c.id, c.workspace_id, c.name, c.topic, c.channel_type,
                      c.mention_policy, c.created_by, c.created_at
               FROM channel c WHERE c.workspace_id = ? AND c.archived_at IS NULL
               AND NOT EXISTS (
                   SELECT 1 FROM direct_conversation d WHERE d.channel_id = c.id
               )""",
            (workspace_id,),
        )
        return [
            Channel(
                id=r["id"],
                workspace_id=r["workspace_id"],
                name=r["name"],
                topic=r["topic"],
                channel_type=ChannelType(r["channel_type"]),
                mention_policy=r["mention_policy"],
                created_by=r["created_by"],
                created_at=_parse(r["created_at"]),
            )
            for r in await cur.fetchall()
        ]

    async def list_channels_for_member(self, member_id: str) -> list[Channel]:
        cur = await self._conn.execute(
            """
            SELECT c.id, c.workspace_id, c.name, c.topic, c.channel_type, c.mention_policy, c.created_by, c.created_at
            FROM channel c
            JOIN channel_member cm ON cm.channel_id = c.id
            WHERE cm.member_id = ? AND cm.is_invitation_pending = 0 AND c.archived_at IS NULL
            """,
            (member_id,),
        )
        return [
            Channel(
                id=r["id"],
                workspace_id=r["workspace_id"],
                name=r["name"],
                topic=r["topic"],
                channel_type=ChannelType(r["channel_type"]),
                mention_policy=r["mention_policy"],
                created_by=r["created_by"],
                created_at=_parse(r["created_at"]),
            )
            for r in await cur.fetchall()
        ]

    async def get_or_create_direct(self, member_id: str, peer_id: str) -> Channel:
        member_a_id, member_b_id = sorted((member_id, peer_id))
        cur = await self._conn.execute(
            "SELECT channel_id FROM direct_conversation WHERE member_a_id = ? AND member_b_id = ?",
            (member_a_id, member_b_id),
        )
        existing = await cur.fetchone()
        if existing:
            channel = await self.get_channel(existing["channel_id"])
            assert channel is not None
            return channel

        peer_cur = await self._conn.execute("SELECT name FROM member WHERE id = ?", (peer_id,))
        peer = await peer_cur.fetchone()
        if peer is None:
            raise ValueError("Direct-message peer not found")
        workspace_cur = await self._conn.execute(
            """SELECT w.id FROM workspace w
               JOIN workspace_member wm ON wm.workspace_id = w.id
               WHERE wm.member_id = ? ORDER BY w.created_at LIMIT 1""",
            (member_id,),
        )
        workspace = await workspace_cur.fetchone()
        if workspace is None:
            raise ValueError("Direct-message sender has no workspace")

        channel_id = f"dm_{uuid.uuid5(uuid.NAMESPACE_URL, member_a_id + ':' + member_b_id).hex}"
        now = dt.datetime.now(dt.timezone.utc).isoformat()
        await self._conn.execute(
            """INSERT INTO channel(id, workspace_id, name, topic, channel_type,
                                    mention_policy, created_by, created_at)
               VALUES (?, ?, ?, 'Direct message', 'permanent', 'channel_members', ?, ?)""",
            (channel_id, workspace["id"], peer["name"], member_id, now),
        )
        await self._conn.execute(
            """INSERT INTO direct_conversation(channel_id, member_a_id, member_b_id, created_at)
               VALUES (?, ?, ?, ?)""",
            (channel_id, member_a_id, member_b_id, now),
        )
        for participant_id in (member_id, peer_id):
            await self._conn.execute(
                """INSERT INTO channel_member(channel_id, member_id, role, joined_at,
                                                invited_by, is_invitation_pending)
                   VALUES (?, ?, 'member', ?, ?, 0)""",
                (channel_id, participant_id, now, member_id),
            )
        channel = await self.get_channel(channel_id)
        assert channel is not None
        return channel

    async def get_direct_peer(self, channel_id: str, member_id: str):
        cur = await self._conn.execute(
            """SELECT m.* FROM direct_conversation d JOIN member m
               ON m.id = CASE WHEN d.member_a_id = ? THEN d.member_b_id ELSE d.member_a_id END
               WHERE d.channel_id = ? AND ? IN (d.member_a_id, d.member_b_id)""",
            (member_id, channel_id, member_id),
        )
        return await cur.fetchone()

    async def list_direct_for_member(self, member_id: str) -> list[dict]:
        cur = await self._conn.execute(
            """SELECT d.channel_id, m.id AS peer_id, m.name, m.avatar, m.kind
               FROM direct_conversation d JOIN member m
               ON m.id = CASE WHEN d.member_a_id = ? THEN d.member_b_id ELSE d.member_a_id END
               WHERE ? IN (d.member_a_id, d.member_b_id) AND m.archived_at IS NULL
               ORDER BY LOWER(m.name)""",
            (member_id, member_id),
        )
        return [dict(row) for row in await cur.fetchall()]

    async def add_member(self, membership: ChannelMembership) -> None:
        joined_at = (
            _iso(membership.joined_at)
            if membership.joined_at
            else dt.datetime.now(dt.timezone.utc).isoformat()
        )
        values = (
            membership.role.value,
            joined_at,
            membership.invited_by,
            int(membership.is_invitation_pending),
            membership.channel_id,
            membership.member_id,
        )
        result = await self._conn.execute(
            """UPDATE channel_member
            SET role = ?, joined_at = ?, invited_by = ?, is_invitation_pending = ?
            WHERE channel_id = ? AND member_id = ?""",
            values,
        )
        if result.rowcount == 0:
            await self._conn.execute(
                """INSERT INTO channel_member
                (channel_id, member_id, role, joined_at, invited_by, is_invitation_pending)
                VALUES (?, ?, ?, ?, ?, ?)""",
                (
                    membership.channel_id,
                    membership.member_id,
                    membership.role.value,
                    joined_at,
                    membership.invited_by,
                    int(membership.is_invitation_pending),
                ),
            )

    async def remove_member(self, channel_id: str, member_id: str) -> None:
        await self._conn.execute(
            "DELETE FROM channel_member WHERE channel_id = ? AND member_id = ?", (channel_id, member_id)
        )

    async def get_membership(self, channel_id: str, member_id: str) -> ChannelMembership | None:
        cur = await self._conn.execute(
            """SELECT channel_id, member_id, role, joined_at, invited_by, is_invitation_pending
            FROM channel_member WHERE channel_id = ? AND member_id = ?""",
            (channel_id, member_id),
        )
        row = await cur.fetchone()
        if not row:
            return None
        return ChannelMembership(
            channel_id=row["channel_id"],
            member_id=row["member_id"],
            role=ChannelRole(row["role"]),
            joined_at=_parse(row["joined_at"]),
            invited_by=row["invited_by"],
            is_invitation_pending=bool(row["is_invitation_pending"]),
        )

    async def list_members(self, channel_id: str) -> list[ChannelMembership]:
        cur = await self._conn.execute(
            """SELECT channel_id, member_id, role, joined_at, invited_by, is_invitation_pending
            FROM channel_member WHERE channel_id = ? AND is_invitation_pending = 0""",
            (channel_id,),
        )
        return [
            ChannelMembership(
                channel_id=r["channel_id"],
                member_id=r["member_id"],
                role=ChannelRole(r["role"]),
                joined_at=_parse(r["joined_at"]),
                invited_by=r["invited_by"],
                is_invitation_pending=bool(r["is_invitation_pending"]),
            )
            for r in await cur.fetchall()
        ]

    async def can_member_access(self, channel_id: str, member_id: str) -> bool:
        cur = await self._conn.execute(
            """SELECT 1 FROM channel_member cm JOIN channel c ON c.id=cm.channel_id
               JOIN workspace w ON w.id=c.workspace_id JOIN team t ON t.id=w.team_id
               WHERE cm.channel_id=? AND cm.member_id=? AND cm.is_invitation_pending=0
               AND c.archived_at IS NULL AND w.archived_at IS NULL AND t.archived_at IS NULL""",
            (channel_id, member_id),
        )
        row = await cur.fetchone()
        return row is not None

    async def can_member_mention(self, channel_id: str, member_id: str, target_id: str) -> bool:
        # First check if member can access the channel
        if not await self.can_member_access(channel_id, member_id):
            return False
        # Check if target is a member of the channel
        cur = await self._conn.execute(
            "SELECT 1 FROM channel_member WHERE channel_id = ? AND member_id = ? AND is_invitation_pending = 0",
            (channel_id, target_id),
        )
        row = await cur.fetchone()
        return row is not None

    async def update_member_role(self, channel_id: str, member_id: str, role: ChannelRole) -> None:
        await self._conn.execute(
            "UPDATE channel_member SET role = ? WHERE channel_id = ? AND member_id = ?",
            (role.value, channel_id, member_id),
        )


class SqlAlchemyWorkflowRepository:
    def __init__(self, conn: SqlAlchemyConnection) -> None:
        self._conn = conn

    @staticmethod
    def _workflow(row) -> Workflow:
        return Workflow(
            id=row["id"], name=row["name"], description=row["description"],
            channel_id=row["channel_id"], enabled=bool(row["enabled"]),
            trigger_type=row["trigger_type"], trigger_config=json.loads(row["trigger_config"]),
            filter_expression=row["filter_expression"], steps=json.loads(row["steps"]),
            creator_id=row["creator_id"], created_at=_parse(row["created_at"]),
            updated_at=_parse(row["updated_at"]),
            next_run_at=_parse(row["next_run_at"]) if row["next_run_at"] else None,
        )

    @staticmethod
    def _run(row) -> WorkflowRun:
        return WorkflowRun(
            id=row["id"], workflow_id=row["workflow_id"], trigger_type=row["trigger_type"],
            event=json.loads(row["event"]), status=WorkflowRunStatus(row["status"]),
            current_step=row["current_step"], step_results=json.loads(row["step_results"]),
            started_at=_parse(row["started_at"]),
            finished_at=_parse(row["finished_at"]) if row["finished_at"] else None,
            error=row["error"], approval_token=row["approval_token"],
            parent_run_id=row["parent_run_id"], root_run_id=row["root_run_id"],
            attempt=row["attempt"], retry_initiated_by=row["retry_initiated_by"],
        )

    async def create(self, workflow: Workflow) -> Workflow:
        now = workflow.created_at or dt.datetime.now(dt.timezone.utc)
        workflow.created_at = workflow.updated_at = now
        await self._conn.execute(
            """INSERT INTO workflow
            (id,name,description,channel_id,enabled,trigger_type,trigger_config,
             filter_expression,steps,creator_id,created_at,updated_at,next_run_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (workflow.id, workflow.name, workflow.description, workflow.channel_id,
             int(workflow.enabled), workflow.trigger_type, json.dumps(workflow.trigger_config),
             workflow.filter_expression, json.dumps(workflow.steps), workflow.creator_id,
             _iso(now), _iso(now), _iso(workflow.next_run_at) if workflow.next_run_at else None),
        )
        return workflow

    async def get(self, workflow_id: str) -> Workflow | None:
        row = await (await self._conn.execute("SELECT * FROM workflow WHERE id=?", (workflow_id,))).fetchone()
        return self._workflow(row) if row else None

    async def get_by_hook_id(self, hook_id: str) -> Workflow | None:
        rows = await (await self._conn.execute(
            "SELECT * FROM workflow WHERE trigger_type='webhook' AND enabled=1"
        )).fetchall()
        return next(
            (self._workflow(row) for row in rows
             if json.loads(row["trigger_config"]).get("hook_id") == hook_id),
            None,
        )

    async def update(self, workflow: Workflow) -> Workflow:
        workflow.updated_at = dt.datetime.now(dt.timezone.utc)
        await self._conn.execute(
            """UPDATE workflow SET name=?,description=?,channel_id=?,enabled=?,trigger_type=?,
               trigger_config=?,filter_expression=?,steps=?,updated_at=?,next_run_at=?,
               claim_token=NULL,claim_until=NULL WHERE id=?""",
            (workflow.name, workflow.description, workflow.channel_id, int(workflow.enabled),
             workflow.trigger_type, json.dumps(workflow.trigger_config), workflow.filter_expression,
             json.dumps(workflow.steps), _iso(workflow.updated_at),
             _iso(workflow.next_run_at) if workflow.next_run_at else None, workflow.id),
        )
        return workflow

    async def delete(self, workflow_id: str) -> None:
        await self._conn.execute("DELETE FROM workflow_run WHERE workflow_id=?", (workflow_id,))
        await self._conn.execute("DELETE FROM workflow WHERE id=?", (workflow_id,))

    async def list_for_channels(self, channel_ids: list[str]) -> list[Workflow]:
        if not channel_ids:
            return []
        marks = ",".join("?" for _ in channel_ids)
        rows = await (await self._conn.execute(
            f"SELECT * FROM workflow WHERE channel_id IN ({marks}) ORDER BY name", channel_ids
        )).fetchall()
        return [self._workflow(row) for row in rows]

    async def list_enabled(self, channel_id: str, trigger_type: str) -> list[Workflow]:
        rows = await (await self._conn.execute(
            "SELECT * FROM workflow WHERE channel_id=? AND trigger_type=? AND enabled=1 ORDER BY created_at",
            (channel_id, trigger_type),
        )).fetchall()
        return [self._workflow(row) for row in rows]

    async def start_run(self, run: WorkflowRun) -> WorkflowRun:
        await self._conn.execute(
            """INSERT INTO workflow_run
            (id,workflow_id,trigger_type,event,status,current_step,step_results,started_at,
             finished_at,error,approval_token,parent_run_id,root_run_id,attempt,retry_initiated_by)
             VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (run.id, run.workflow_id, run.trigger_type, json.dumps(run.event), run.status.value,
             run.current_step, json.dumps(run.step_results), _iso(run.started_at),
             _iso(run.finished_at) if run.finished_at else None, run.error, run.approval_token,
             run.parent_run_id, run.root_run_id, run.attempt, run.retry_initiated_by),
        )
        return run

    async def update_run(self, run: WorkflowRun) -> None:
        await self._conn.execute(
            """UPDATE workflow_run SET status=?,current_step=?,step_results=?,finished_at=?,
               error=?,approval_token=? WHERE id=?""",
            (run.status.value, run.current_step, json.dumps(run.step_results),
             _iso(run.finished_at) if run.finished_at else None, run.error,
             run.approval_token, run.id),
        )

    async def get_run(self, run_id: str) -> WorkflowRun | None:
        row = await (await self._conn.execute("SELECT * FROM workflow_run WHERE id=?", (run_id,))).fetchone()
        return self._run(row) if row else None

    async def get_run_by_approval(self, token: str) -> WorkflowRun | None:
        row = await (await self._conn.execute(
            "SELECT * FROM workflow_run WHERE approval_token=?", (token,)
        )).fetchone()
        return self._run(row) if row else None

    async def list_pending_approvals(self, workflow_ids: list[str]) -> list[WorkflowRun]:
        if not workflow_ids:
            return []
        placeholders = ",".join("?" for _ in workflow_ids)
        rows = await (await self._conn.execute(
            f"SELECT * FROM workflow_run WHERE status='waiting' "
            f"AND workflow_id IN ({placeholders}) ORDER BY started_at DESC",
            tuple(workflow_ids),
        )).fetchall()
        return [self._run(row) for row in rows]

    async def list_runs(self, workflow_id: str) -> list[WorkflowRun]:
        rows = await (await self._conn.execute(
            "SELECT * FROM workflow_run WHERE workflow_id=? ORDER BY started_at DESC", (workflow_id,)
        )).fetchall()
        return [self._run(row) for row in rows]

    async def list_due_schedules(self, now: dt.datetime) -> list[Workflow]:
        rows = await (await self._conn.execute(
            "SELECT * FROM workflow WHERE enabled=1 AND trigger_type='schedule' AND next_run_at<=?",
            (_iso(now),),
        )).fetchall()
        return [self._workflow(row) for row in rows]

    async def claim_due_schedules(
        self, now: dt.datetime, *, claim_token: str, claim_until: dt.datetime
    ) -> list[Workflow]:
        rows = await (await self._conn.execute(
            """UPDATE workflow
               SET claim_token=?, claim_until=?
               WHERE id IN (
                   SELECT workflow.id FROM workflow
                   JOIN channel ON channel.id=workflow.channel_id
                   JOIN workspace ON workspace.id=channel.workspace_id
                   JOIN team ON team.id=workspace.team_id
                   WHERE workflow.enabled=1 AND workflow.trigger_type='schedule'
                   AND workflow.next_run_at<=?
                   AND (workflow.claim_until IS NULL OR workflow.claim_until<=?)
                   AND channel.archived_at IS NULL AND workspace.archived_at IS NULL
                   AND team.archived_at IS NULL
               )
               RETURNING *""",
            (claim_token, _iso(claim_until), _iso(now), _iso(now)),
        )).fetchall()
        return [self._workflow(row) for row in rows]


class SqlAlchemyBoardRepository:
    def __init__(self, conn: SqlAlchemyConnection) -> None:
        self._conn = conn

    async def get_board(self, board_id: str) -> BoardView | None:
        cur = await self._conn.execute("SELECT id, workspace_id, name FROM board WHERE id = ?", (board_id,))
        row = await cur.fetchone()
        if not row:
            return None
        board = BoardView(id=row["id"], workspace_id=row["workspace_id"], name=row["name"])
        ccur = await self._conn.execute(
            "SELECT id, board_id, name, position FROM board_column WHERE board_id = ? ORDER BY position ASC", (board_id,)
        )
        for col in await ccur.fetchall():
            board.columns.append(await self._column_with_cards(col))
        return board

    async def list_all(self) -> list[BoardView]:
        cur = await self._conn.execute(
            """
            SELECT b.id, b.workspace_id, b.name, t.name AS team_name
            FROM board b
            JOIN workspace w ON w.id = b.workspace_id
            LEFT JOIN team t ON t.id = w.team_id
            ORDER BY b.name ASC
            """
        )
        return [
            BoardView(
                id=r["id"], workspace_id=r["workspace_id"], name=r["name"],
                team_name=r["team_name"],
            )
            for r in await cur.fetchall()
        ]

    async def list_for_member(self, member_id: str) -> list[BoardView]:
        cur = await self._conn.execute(
            """
            SELECT b.id, b.workspace_id, b.name, t.name AS team_name
            FROM board b
            JOIN workspace w ON w.id = b.workspace_id
            LEFT JOIN team t ON t.id = w.team_id
            JOIN workspace_member wm ON wm.workspace_id = w.id AND wm.member_id = ?
            ORDER BY b.name ASC
            """,
            (member_id,),
        )
        return [
            BoardView(
                id=r["id"], workspace_id=r["workspace_id"], name=r["name"],
                team_name=r["team_name"],
            )
            for r in await cur.fetchall()
        ]

    async def get_board_id_for_column(self, column_id: str) -> str | None:
        row = await (
            await self._conn.execute(
                "SELECT board_id FROM board_column WHERE id = ?", (column_id,)
            )
        ).fetchone()
        return row["board_id"] if row else None

    async def get_board_id_for_card(self, card_id: str) -> str | None:
        row = await (
            await self._conn.execute(
                """SELECT bc.board_id FROM card c
                   JOIN board_column bc ON bc.id = c.column_id
                   WHERE c.id = ?""",
                (card_id,),
            )
        ).fetchone()
        return row["board_id"] if row else None

    async def _column_with_cards(self, col) -> ColumnView:
        ccur = await self._conn.execute(
            """
            SELECT c.id, c.column_id, c.title, c.description, c.position, c.assignee_id,
                   mem.name AS assignee_name, mem.avatar AS assignee_avatar,
                   c.created_by, c.updated_by, c.updated_at,
                   cb.name AS created_by_name, ub.name AS updated_by_name
            FROM card c
            LEFT JOIN member mem ON mem.id = c.assignee_id
            LEFT JOIN member cb ON cb.id = c.created_by
            LEFT JOIN member ub ON ub.id = c.updated_by
            WHERE c.column_id = ? ORDER BY c.position ASC
            """,
            (col["id"],),
        )
        cards = []
        for r in await ccur.fetchall():
            comments = await self._comments(r["id"])
            cards.append(
                CardView(
                    id=r["id"],
                    column_id=r["column_id"],
                    title=r["title"],
                    description=r["description"],
                    assignee_id=r["assignee_id"],
                    position=r["position"],
                    assignee_name=r["assignee_name"],
                    assignee_avatar=r["assignee_avatar"],
                    created_by=r["created_by"],
                    updated_by=r["updated_by"],
                    updated_at=r["updated_at"],
                    created_by_name=r["created_by_name"],
                    updated_by_name=r["updated_by_name"],
                    comments=comments,
                )
            )
        return ColumnView(
            id=col["id"], board_id=col["board_id"], name=col["name"], position=col["position"], cards=cards
        )

    async def _comments(self, card_id: str) -> list[CommentView]:
        ccur = await self._conn.execute(
            """
            SELECT cc.id, cc.card_id, cc.author_id, cc.body, cc.created_at,
                   mem.name AS author_name, mem.kind AS author_kind, mem.avatar
            FROM card_comment cc JOIN member mem ON mem.id = cc.author_id
            WHERE cc.card_id = ? ORDER BY cc.created_at ASC
            """,
            (card_id,),
        )
        return [
            CommentView(
                id=r["id"],
                card_id=r["card_id"],
                author_id=r["author_id"],
                body=r["body"],
                created_at=_parse(r["created_at"]),
                author_name=r["author_name"],
                author_kind=MemberKind(r["author_kind"]),
                author_avatar=r["avatar"],
            )
            for r in await ccur.fetchall()
        ]

    async def add_card(
        self, column_id: str, title: str, description: str | None = None, actor_id: str | None = None
    ) -> CardView:
        cid = uuid.uuid4().hex
        now = dt.datetime.now(dt.timezone.utc).isoformat()
        cur = await self._conn.execute(
            "SELECT COALESCE(MAX(position), -1) + 1 AS pos FROM card WHERE column_id = ?", (column_id,)
        )
        pos = (await cur.fetchone())["pos"]
        await self._conn.execute(
            "INSERT INTO card (id, column_id, title, description, position, created_by, updated_by, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (cid, column_id, title, description, pos, actor_id, actor_id, now),
        )
        return await self.get_card(cid)  # type: ignore[return-value]

    async def get_card(self, card_id: str) -> CardView | None:
        cur = await self._conn.execute(
            """
            SELECT c.id, c.column_id, c.title, c.description, c.position, c.assignee_id,
                   mem.name AS assignee_name, mem.avatar AS assignee_avatar,
                   c.created_by, c.updated_by, c.updated_at,
                   cb.name AS created_by_name, ub.name AS updated_by_name
            FROM card c
            LEFT JOIN member mem ON mem.id = c.assignee_id
            LEFT JOIN member cb ON cb.id = c.created_by
            LEFT JOIN member ub ON ub.id = c.updated_by
            WHERE c.id = ?
            """,
            (card_id,),
        )
        row = await cur.fetchone()
        if not row:
            return None
        return CardView(
            id=row["id"],
            column_id=row["column_id"],
            title=row["title"],
            description=row["description"],
            assignee_id=row["assignee_id"],
            position=row["position"],
            assignee_name=row["assignee_name"],
            assignee_avatar=row["assignee_avatar"],
            created_by=row["created_by"],
            updated_by=row["updated_by"],
            updated_at=row["updated_at"],
            created_by_name=row["created_by_name"],
            updated_by_name=row["updated_by_name"],
            comments=await self._comments(row["id"]),
        )

    async def move_card(self, card_id: str, column_id: str, actor_id: str | None = None) -> CardView | None:
        now = dt.datetime.now(dt.timezone.utc).isoformat()
        await self._conn.execute(
            "UPDATE card SET column_id = ?, position = (SELECT COALESCE(MAX(position), -1) + 1 FROM card WHERE column_id = ?), updated_by = ?, updated_at = ? WHERE id = ?",
            (column_id, column_id, actor_id, now, card_id),
        )
        return await self.get_card(card_id)

    async def add_comment(self, card_id: str, author_id: str, body: str) -> CommentView:
        cid = uuid.uuid4().hex
        now = dt.datetime.now(dt.timezone.utc).isoformat()
        await self._conn.execute(
            "INSERT INTO card_comment (id, card_id, author_id, body, created_at) VALUES (?, ?, ?, ?, ?)",
            (cid, card_id, author_id, body, now),
        )
        cur = await self._conn.execute(
            """
            SELECT cc.id, cc.card_id, cc.author_id, cc.body, cc.created_at,
                   mem.name AS author_name, mem.kind AS author_kind, mem.avatar
            FROM card_comment cc JOIN member mem ON mem.id = cc.author_id WHERE cc.id = ?
            """,
            (cid,),
        )
        r = await cur.fetchone()
        assert r is not None  # row was just inserted
        return CommentView(
            id=r["id"],
            card_id=r["card_id"],
            author_id=r["author_id"],
            body=r["body"],
            created_at=_parse(r["created_at"]),
            author_name=r["author_name"],
            author_kind=MemberKind(r["author_kind"]),
            author_avatar=r["avatar"],
        )

    async def find_card_by_title(self, board_id: str, title: str) -> CardView | None:
        board = await self.get_board(board_id)
        if not board:
            return None
        for col in board.columns:
            for c in col.cards:
                if c.title.lower() == title.lower():
                    return c
        return None

    async def list_columns(self, board_id: str) -> dict[str, str]:
        cur = await self._conn.execute(
            "SELECT id, name FROM board_column WHERE board_id = ?", (board_id,)
        )
        return {r["name"].lower(): r["id"] for r in await cur.fetchall()}