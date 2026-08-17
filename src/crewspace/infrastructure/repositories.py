"""Infrastructure: sqlite repository implementations.

Each method maps DB rows -> domain view entities. This is the single place
where SQL and the domain model meet. A Postgres port would reimplement these
against asyncpg while returning the exact same entities, so every layer above
(application services, API) is untouched.
"""
from __future__ import annotations

import datetime as dt
import json
import uuid

from .sql import MappingRow, SqlAlchemyConnection

from ..domain.entities import (
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