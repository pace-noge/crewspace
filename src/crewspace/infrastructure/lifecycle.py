"""Archival and explicit permanent-deletion plans for managed entities."""
from __future__ import annotations

import datetime as dt

from .sql import SqlAlchemyConnection


class SqlAlchemyLifecycleRepository:
    def __init__(self, conn: SqlAlchemyConnection) -> None:
        self._conn = conn

    async def get(self, kind: str, entity_id: str):
        table = self._table(kind)
        cur = await self._conn.execute(
            f"SELECT id, name, archived_at FROM {table} WHERE id = ?", (entity_id,)
        )
        row = await cur.fetchone()
        if row is None or (kind == "agent" and await self._kind(entity_id) != "agent"):
            return None
        return row

    async def set_archived(self, kind: str, entity_id: str, archived: bool) -> None:
        table = self._table(kind)
        value = dt.datetime.now(dt.timezone.utc).isoformat() if archived else None
        await self._conn.execute(
            f"UPDATE {table} SET archived_at = ? WHERE id = ?", (value, entity_id)
        )

    async def list_archived(self) -> list[dict]:
        result: list[dict] = []
        for kind, table, collection in (
            ("team", "team", "teams"),
            ("workspace", "workspace", "workspaces"),
            ("channel", "channel", "channels"),
            ("agent", "member", "agents"),
        ):
            where = "archived_at IS NOT NULL"
            if kind == "agent":
                where += " AND kind='agent'"
            cur = await self._conn.execute(
                f"SELECT id,name,archived_at FROM {table} WHERE {where} ORDER BY name"
            )
            result.extend(
                {**dict(row), "kind": kind, "collection": collection}
                for row in await cur.fetchall()
            )
        return result

    async def dependency_counts(self, kind: str, entity_id: str) -> dict[str, int]:
        if kind == "channel":
            return await self._channel_counts([entity_id])
        if kind == "workspace":
            channels = await self._ids("SELECT id FROM channel WHERE workspace_id=?", entity_id)
            counts = await self._channel_counts(channels)
            counts["channels"] = len(channels)
            counts["boards"] = await self._count("SELECT COUNT(*) FROM board WHERE workspace_id=?", entity_id)
            return counts
        if kind == "team":
            workspaces = await self._ids("SELECT id FROM workspace WHERE team_id=?", entity_id)
            channels = await self._ids(
                "SELECT c.id FROM channel c JOIN workspace w ON w.id=c.workspace_id WHERE w.team_id=?",
                entity_id,
            )
            counts = await self._channel_counts(channels)
            counts["workspaces"] = len(workspaces)
            counts["channels"] = len(channels)
            counts["boards"] = await self._count(
                "SELECT COUNT(*) FROM board b JOIN workspace w ON w.id=b.workspace_id WHERE w.team_id=?",
                entity_id,
            )
            return counts
        doomed_messages = await self._agent_message_ids(entity_id)
        return {
            "messages and thread replies": len(doomed_messages),
            "direct messages": await self._count(
                "SELECT COUNT(*) FROM direct_conversation WHERE member_a_id=? OR member_b_id=?",
                entity_id, entity_id,
            ),
            "reactions": await self._count("SELECT COUNT(*) FROM message_reaction WHERE member_id=?", entity_id),
            "card assignments": await self._count("SELECT COUNT(*) FROM card WHERE assignee_id=?", entity_id),
            "scheduled instructions": await self._count("SELECT COUNT(*) FROM scheduled_job WHERE creator_id=?", entity_id),
            "creator-owned channels": await self._count(
                "SELECT COUNT(*) FROM channel WHERE created_by=?", entity_id
            ),
            "creator-owned workspaces": await self._count(
                "SELECT COUNT(*) FROM workspace WHERE created_by=?", entity_id
            ),
            "creator-owned teams": await self._count(
                "SELECT COUNT(*) FROM team WHERE created_by=?", entity_id
            ),
        }

    async def delete_permanently(self, kind: str, entity_id: str) -> None:
        if kind == "channel":
            await self._delete_channel(entity_id)
        elif kind == "workspace":
            await self._delete_workspace(entity_id)
        elif kind == "team":
            for workspace_id in await self._ids("SELECT id FROM workspace WHERE team_id=?", entity_id):
                await self._delete_workspace(workspace_id)
            await self._conn.execute("DELETE FROM team_member WHERE team_id=?", (entity_id,))
            await self._conn.execute("DELETE FROM team WHERE id=?", (entity_id,))
        elif kind == "agent":
            await self._delete_agent(entity_id)
        else:
            raise ValueError("Unknown lifecycle entity")

    async def team_scope(self, kind: str, entity_id: str) -> str | None:
        if kind == "team":
            return entity_id
        if kind == "workspace":
            cur = await self._conn.execute("SELECT team_id FROM workspace WHERE id=?", (entity_id,))
        elif kind == "channel":
            cur = await self._conn.execute(
                "SELECT w.team_id FROM channel c JOIN workspace w ON w.id=c.workspace_id WHERE c.id=?",
                (entity_id,),
            )
        else:
            return None
        row = await cur.fetchone()
        return row[0] if row else None

    async def can_manage_archived_team(self, team_id: str, member_id: str, role: str) -> bool:
        cur = await self._conn.execute(
            "SELECT role FROM team_member WHERE team_id=? AND member_id=?",
            (team_id, member_id),
        )
        row = await cur.fetchone()
        if role == "engineering_manager":
            return row is not None
        return row is not None and row["role"] == "leader"

    async def _delete_agent(self, agent_id: str) -> None:
        dm_channels = await self._ids(
            "SELECT channel_id FROM direct_conversation WHERE member_a_id=? OR member_b_id=?",
            agent_id, agent_id,
        )
        for channel_id in dm_channels:
            await self._delete_channel(channel_id)
        created_channels = await self._ids("SELECT id FROM channel WHERE created_by=?", agent_id)
        for channel_id in created_channels:
            await self._delete_channel(channel_id)
        created_workspaces = await self._ids("SELECT id FROM workspace WHERE created_by=?", agent_id)
        for workspace_id in created_workspaces:
            await self._delete_workspace(workspace_id)
        created_teams = await self._ids("SELECT id FROM team WHERE created_by=?", agent_id)
        for team_id in created_teams:
            await self.delete_permanently("team", team_id)
        message_ids = await self._agent_message_ids(agent_id)
        if message_ids:
            marks = ",".join("?" for _ in message_ids)
            await self._conn.execute(f"DELETE FROM message_reaction WHERE message_id IN ({marks})", message_ids)
            await self._conn.execute(f"DELETE FROM message WHERE id IN ({marks})", message_ids)
        await self._conn.execute("DELETE FROM message_reaction WHERE member_id=?", (agent_id,))
        await self._conn.execute("DELETE FROM card_comment WHERE author_id=?", (agent_id,))
        await self._conn.execute(
            "UPDATE card SET assignee_id=NULL WHERE assignee_id=?", (agent_id,)
        )
        await self._conn.execute(
            "UPDATE card SET created_by=NULL WHERE created_by=?", (agent_id,)
        )
        await self._conn.execute(
            "UPDATE card SET updated_by=NULL WHERE updated_by=?", (agent_id,)
        )
        job_ids = await self._ids("SELECT id FROM scheduled_job WHERE creator_id=?", agent_id)
        for job_id in job_ids:
            await self._conn.execute("DELETE FROM scheduled_job_run WHERE job_id=?", (job_id,))
        await self._conn.execute("DELETE FROM scheduled_job WHERE creator_id=?", (agent_id,))
        await self._conn.execute("DELETE FROM session WHERE member_id=?", (agent_id,))
        await self._conn.execute("DELETE FROM channel_member WHERE member_id=?", (agent_id,))
        await self._conn.execute("DELETE FROM workspace_member WHERE member_id=?", (agent_id,))
        await self._conn.execute("DELETE FROM team_member WHERE member_id=?", (agent_id,))
        await self._conn.execute("DELETE FROM member WHERE id=? AND kind='agent'", (agent_id,))

    async def _agent_message_ids(self, agent_id: str) -> list[str]:
        """Messages authored by an agent plus every reply below those roots."""
        return await self._ids(
            """
            WITH RECURSIVE doomed(id) AS (
                SELECT id FROM message WHERE author_id=?
                UNION
                SELECT m.id FROM message m JOIN doomed d ON m.thread_id=d.id
            )
            SELECT id FROM doomed
            """,
            agent_id,
        )

    async def _delete_workspace(self, workspace_id: str) -> None:
        for channel_id in await self._ids("SELECT id FROM channel WHERE workspace_id=?", workspace_id):
            await self._delete_channel(channel_id)
        board_ids = await self._ids("SELECT id FROM board WHERE workspace_id=?", workspace_id)
        for board_id in board_ids:
            column_ids = await self._ids("SELECT id FROM board_column WHERE board_id=?", board_id)
            for column_id in column_ids:
                card_ids = await self._ids("SELECT id FROM card WHERE column_id=?", column_id)
                for card_id in card_ids:
                    await self._conn.execute("DELETE FROM card_comment WHERE card_id=?", (card_id,))
                await self._conn.execute("DELETE FROM card WHERE column_id=?", (column_id,))
            await self._conn.execute("DELETE FROM board_column WHERE board_id=?", (board_id,))
        await self._conn.execute("DELETE FROM board WHERE workspace_id=?", (workspace_id,))
        await self._conn.execute("DELETE FROM workspace_member WHERE workspace_id=?", (workspace_id,))
        await self._conn.execute("DELETE FROM workspace WHERE id=?", (workspace_id,))

    async def _delete_channel(self, channel_id: str) -> None:
        job_ids = await self._ids("SELECT id FROM scheduled_job WHERE channel_id=?", channel_id)
        for job_id in job_ids:
            await self._conn.execute("DELETE FROM scheduled_job_run WHERE job_id=?", (job_id,))
        await self._conn.execute("DELETE FROM scheduled_job_run WHERE channel_id=?", (channel_id,))
        await self._conn.execute("DELETE FROM scheduled_job WHERE channel_id=?", (channel_id,))
        message_ids = await self._ids("SELECT id FROM message WHERE channel_id=?", channel_id)
        if message_ids:
            marks = ",".join("?" for _ in message_ids)
            await self._conn.execute(f"DELETE FROM message_reaction WHERE message_id IN ({marks})", message_ids)
        await self._conn.execute("DELETE FROM message WHERE channel_id=?", (channel_id,))
        await self._conn.execute("DELETE FROM direct_conversation WHERE channel_id=?", (channel_id,))
        await self._conn.execute("DELETE FROM channel_member WHERE channel_id=?", (channel_id,))
        await self._conn.execute("DELETE FROM channel WHERE id=?", (channel_id,))

    async def _channel_counts(self, channel_ids: list[str]) -> dict[str, int]:
        if not channel_ids:
            return {"messages": 0, "scheduled instructions": 0, "memberships": 0}
        marks = ",".join("?" for _ in channel_ids)
        return {
            "messages": await self._count(f"SELECT COUNT(*) FROM message WHERE channel_id IN ({marks})", *channel_ids),
            "scheduled instructions": await self._count(f"SELECT COUNT(*) FROM scheduled_job WHERE channel_id IN ({marks})", *channel_ids),
            "memberships": await self._count(f"SELECT COUNT(*) FROM channel_member WHERE channel_id IN ({marks})", *channel_ids),
        }

    async def _kind(self, member_id: str) -> str | None:
        cur = await self._conn.execute("SELECT kind FROM member WHERE id=?", (member_id,))
        row = await cur.fetchone()
        return row["kind"] if row else None

    async def _ids(self, sql: str, *params: str) -> list[str]:
        cur = await self._conn.execute(sql, params)
        return [row["id"] if "id" in row.keys() else row[0] for row in await cur.fetchall()]

    async def _count(self, sql: str, *params: str) -> int:
        cur = await self._conn.execute(sql, params)
        row = await cur.fetchone()
        assert row is not None
        return int(row[0])

    @staticmethod
    def _table(kind: str) -> str:
        tables = {"team": "team", "workspace": "workspace", "channel": "channel", "agent": "member"}
        if kind not in tables:
            raise ValueError("Unknown lifecycle entity")
        return tables[kind]
