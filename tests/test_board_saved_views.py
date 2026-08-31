"""M7.6 — persisted, authorization-scoped saved board views (RED first)."""
from __future__ import annotations

import datetime as dt

import pytest

from crewspace.application.board_views import BoardSavedViewService
from crewspace.domain.entities import WorkspaceMembership, WorkspaceRole
from crewspace.dto.board import BoardFilterDTO, BoardGroupDTO


async def _seed_alice(app) -> dict:
    async with app.state.db.uow() as uow:
        await uow.auth.create_member(
            "user_alice", "human", "Alice", "temporary-password", "team_member"
        )
        await uow.workspaces.add_member(
            WorkspaceMembership(
                workspace_id="ws_default",
                member_id="user_alice",
                role=WorkspaceRole.MEMBER,
                joined_at=dt.datetime.now(dt.timezone.utc),
            )
        )
        await uow.commit()
    async with app.state.db.uow() as uow:
        return await uow.auth.get_member("user_alice")


@pytest.mark.asyncio
async def test_saved_view_persists_for_owner_and_round_trips(app):
    async with app.state.db.uow() as uow:
        owner = await uow.auth.get_member("user_bilal")
        assert owner is not None
        saved = await BoardSavedViewService().save(
            board_id="board_main",
            owner=owner,
            name="Urgent agent work",
            view="swimlane",
            filters=BoardFilterDTO(agent_id="agent_planner", priority="urgent"),
            group=BoardGroupDTO(by="agent"),
            uow=uow,
        )
        await uow.commit()

    async with app.state.db.uow() as uow:
        owner = await uow.auth.get_member("user_bilal")
        assert owner is not None
        views = await BoardSavedViewService().list_for_board("board_main", owner, uow)

    assert [v.id for v in views] == [saved.id]
    assert views[0].name == "Urgent agent work"
    assert views[0].view == "swimlane"
    assert views[0].filters.agent_id == "agent_planner"
    assert views[0].filters.priority == "urgent"
    assert views[0].group == BoardGroupDTO(by="agent")


@pytest.mark.asyncio
async def test_saved_view_is_owner_scoped_even_for_same_board_member(app):
    alice = await _seed_alice(app)
    async with app.state.db.uow() as uow:
        owner = await uow.auth.get_member("user_bilal")
        assert owner is not None
        saved = await BoardSavedViewService().save(
            board_id="board_main",
            owner=owner,
            name="Mine",
            view="timeline",
            filters=BoardFilterDTO(due="overdue"),
            group=None,
            uow=uow,
        )
        await uow.commit()

    # Alice is a member of the same workspace/board, so she *can* access the
    # board — yet she must NOT read or delete Bilal's private saved view.
    async with app.state.db.uow() as uow:
        assert await BoardSavedViewService().get(saved.id, alice, uow) is None
        assert await BoardSavedViewService().list_for_board("board_main", alice, uow) == []
        with pytest.raises(PermissionError):
            await BoardSavedViewService().delete(saved.id, alice, uow)
        # The owner can still see and delete their own view.
        owner = await uow.auth.get_member("user_bilal")
        assert owner is not None
        assert await BoardSavedViewService().get(saved.id, owner, uow) is not None
        await BoardSavedViewService().delete(saved.id, owner, uow)
        await uow.commit()

    async with app.state.db.uow() as uow:
        owner = await uow.auth.get_member("user_bilal")
        assert owner is not None
        assert await BoardSavedViewService().list_for_board("board_main", owner, uow) == []


@pytest.mark.asyncio
async def test_saved_view_rejects_blank_name_without_inserting(app):
    """A blank name must fail validation BEFORE any row is persisted."""
    async with app.state.db.uow() as uow:
        owner = await uow.auth.get_member("user_bilal")
        assert owner is not None
        with pytest.raises(Exception):
            await BoardSavedViewService().save(
                board_id="board_main",
                owner=owner,
                name="   ",
                view="board",
                uow=uow,
            )
        # Nothing was inserted under the blank name.
        views = await BoardSavedViewService().list_for_board("board_main", owner, uow)
    assert views == []


def test_m76_migration_roundtrip_fresh_legacy_and_populated_downgrade():
    """M7.6's board_saved_view migration converges on every upgrade path."""
    import asyncio
    import os
    import sqlite3
    import subprocess
    import sys
    import tempfile

    from crewspace.config import Settings
    from crewspace.infrastructure.db import Database

    def run_alembic(command: str, target: str, url: str) -> None:
        env = {**os.environ, "CREWSPACE_DATABASE_URL": url}
        result = subprocess.run(
            [sys.executable, "-m", "alembic", "-c", "alembic.ini", command, target],
            capture_output=True,
            text=True,
            timeout=120,
            env=env,
        )
        if result.returncode != 0:
            raise AssertionError(result.stdout + result.stderr)

    def has_saved_views_table(db_path: str) -> bool:
        with sqlite3.connect(db_path) as conn:
            row = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='board_saved_view'"
            ).fetchone()
        return row is not None

    with tempfile.TemporaryDirectory() as tmp:
        db_path = os.path.join(tmp, "m76.db")
        url = f"sqlite+aiosqlite:///{db_path}"

        async def create_at_head() -> None:
            db = await Database.create(Settings(database_url=url))
            await db.close()

        # Fresh DB at head has the table.
        asyncio.run(create_at_head())
        assert has_saved_views_table(db_path)

        # Legacy: downgrade to the M7.5 head removes ONLY the M7.6 table, then
        # upgrade head recreates it exactly once (idempotent).
        run_alembic("downgrade", "20260830_02", url)
        assert not has_saved_views_table(db_path)
        run_alembic("upgrade", "head", url)
        assert has_saved_views_table(db_path)

        # Populated downgrade: seed a saved view at head, downgrade to M7.5
        # head (table dropped), then upgrade head again.
        async def seed_view() -> None:
            db = await Database.create(Settings(database_url=url))
            async with db.uow() as uow:
                await uow.boards.add_saved_view(
                    __import__(
                        "crewspace.domain.entities", fromlist=["SavedBoardView"]
                    ).SavedBoardView(
                        id="view_seed",
                        board_id="board_main",
                        owner_id="user_bilal",
                        name="seed",
                        view="board",
                        filters=None,
                        group=None,
                        created_at=dt.datetime(2026, 8, 31, tzinfo=dt.timezone.utc),
                    )
                )
                await uow.commit()
            await db.close()

        async def count_views(db_path: str) -> int:
            import aiosqlite

            conn = await aiosqlite.connect(db_path)
            cur = await conn.execute("SELECT COUNT(*) FROM board_saved_view")
            (n,) = await cur.fetchone()
            await conn.close()
            return n

        asyncio.run(seed_view())
        assert asyncio.run(count_views(db_path)) == 1
        run_alembic("downgrade", "20260830_02", url)
        assert not has_saved_views_table(db_path)
        run_alembic("upgrade", "head", url)
        assert has_saved_views_table(db_path)
