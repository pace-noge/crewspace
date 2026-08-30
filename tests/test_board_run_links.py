"""M7.4 — card ↔ coding-run / change-set linkage (RED first).

The core tracer is intentionally service-level over the real SQLite UoW:
create a card and a scoped coding run, link them from authenticated state, then
read a live status projection. Cross-team links fail closed and duplicate link
retries remain idempotent.
"""
from __future__ import annotations

import datetime as dt

import pytest

from crewspace.application.services import BoardService
from crewspace.application.tools import build_registry
from crewspace.domain.entities import CodingRepository, CodingRun, TeamRepositoryAccess


async def _seed_card_and_run(app, *, run_id: str = "run_card_link_1"):
    now = dt.datetime.now(dt.timezone.utc)
    async with app.state.db.uow() as uow:
        card = await uow.boards.add_card(
            "col_todo", "Implement linked card", actor_id="user_bilal"
        )
        await uow.coding_repositories.create(
            CodingRepository(
                id="repo_card_link",
                name="Card Link Repo",
                default_branch="main",
                created_by="user_bilal",
                created_at=now,
            )
        )
        await uow.coding_repositories.grant_team(
            TeamRepositoryAccess(
                team_id="team_acme",
                repository_id="repo_card_link",
                granted_by="user_bilal",
                granted_at=now,
            )
        )
        await uow.coding_runs.create(
            CodingRun(
                id=run_id,
                team_id="team_acme",
                repository_id="repo_card_link",
                requested_by="user_bilal",
                agent_id="agent_planner",
                request_id=f"req_{run_id}",
                instruction="Implement the linked card",
                status="running",
                created_at=now,
                updated_at=now,
                started_at=now,
            )
        )
        user = await uow.auth.get_member("user_bilal")
        await uow.commit()
        return card, user


async def test_authenticated_card_run_link_exposes_live_status(app):
    card, user = await _seed_card_and_run(app)
    service = BoardService(build_registry(), app.state.settings)

    async with app.state.db.uow() as uow:
        linked = await service.link_card_to_run(
            card.id, "run_card_link_1", user, uow
        )
        # Same authenticated retry is idempotent (one durable link only).
        linked_again = await service.link_card_to_run(
            card.id, "run_card_link_1", user, uow
        )
        statuses = await service.card_run_status(card.id, user, uow)
        await uow.commit()

    assert linked.card_id == card.id
    assert linked.run_id == "run_card_link_1"
    assert linked.linked_by == "user_bilal"
    assert linked_again == linked
    assert len(statuses) == 1
    assert statuses[0].run_id == "run_card_link_1"
    assert statuses[0].run_status == "running"
    assert statuses[0].change_set_id is None
    assert statuses[0].change_set_status is None


async def test_card_run_link_rejects_cross_team_run(app):
    card, user = await _seed_card_and_run(app, run_id="run_cross_team")
    service = BoardService(build_registry(), app.state.settings)

    async with app.state.db.uow() as uow:
        run = await uow.coding_runs.get("run_cross_team")
        assert run is not None
        # Simulate a tampered/foreign run identity: the service must derive the
        # card's team from board→workspace and refuse the mismatch.
        await uow._conn.execute(
            "UPDATE coding_run SET team_id='team_foreign' WHERE id=?",
            (run.id,),
        )
        with pytest.raises(PermissionError, match="team"):
            await service.link_card_to_run(card.id, run.id, user, uow)

        assert await service.card_run_status(card.id, user, uow) == []
        await uow.rollback()


async def test_card_run_status_reveals_nothing_without_board_access(app):
    card, user = await _seed_card_and_run(app, run_id="run_private_link")
    service = BoardService(build_registry(), app.state.settings)

    async with app.state.db.uow() as uow:
        await service.link_card_to_run(card.id, "run_private_link", user, uow)
        outsider = {"id": "user_outsider", "role": "human"}
        # Fail-closed: an outsider sees NO linked-run data, not a hint.
        assert await service.card_run_status(card.id, outsider, uow) == []
        await uow.rollback()


def test_card_run_status_dto_is_pure_and_strict():
    from pydantic import ValidationError

    from crewspace.dto.board import CardRunStatusDTO

    dto = CardRunStatusDTO(
        card_id="card_1",
        run_id="run_1",
        run_status="running",
        linked_by="user_bilal",
        linked_at=dt.datetime.now(dt.timezone.utc),
    )
    assert dto.change_set_id is None
    with pytest.raises(ValidationError):
        CardRunStatusDTO(
            card_id="card_1",
            run_id="run_1",
            run_status="unknown",
            linked_by="user_bilal",
            linked_at=dt.datetime.now(dt.timezone.utc),
        )
    with pytest.raises(ValidationError):
        CardRunStatusDTO(
            card_id="card_1",
            run_id="run_1",
            run_status="running",
            linked_by="user_bilal",
            linked_at=dt.datetime.now(dt.timezone.utc),
            unexpected=True,
        )


def test_m74_migration_is_head_and_models_are_in_sync():
    """Acceptance: the new link table is migration-guarded and models match."""
    import asyncio
    import os
    import subprocess
    import sys
    import tempfile

    from crewspace.config import Settings
    from crewspace.infrastructure.db import Database

    # Upgrade a BRAND-NEW temp DB to head (unaffected by the dev DB), then run
    # the check against it — deterministic and independent of local state.
    with tempfile.TemporaryDirectory() as tmp:
        db_path = os.path.join(tmp, "m74.db")
        url = f"sqlite+aiosqlite:///{db_path}"
        # Point Settings at the temp DB without leaking a persistent env var.
        settings = Settings(database_url=url)

        async def upgrade_and_close() -> None:
            db = await Database.create(settings)
            await db.close()

        asyncio.run(upgrade_and_close())
        env = {**os.environ, "CREWSPACE_DATABASE_URL": url}
        result = subprocess.run(
            [sys.executable, "-m", "crewspace.management.cli", "makemigrations", "--check"],
            capture_output=True,
            text=True,
            timeout=120,
            env=env,
        )
        assert result.returncode == 0, result.stdout + result.stderr
        assert "20260830_02" in result.stdout
        assert "No changes detected" in result.stdout


def test_m74_legacy_db_upgrade_roundtrip():
    """A DB at 20260826_02 gains the link table via the new migration.

    Exercises the populated LEGACY-state upgrade (downgrade to old head, insert
    legacy rows, upgrade) so an existing deployment at the prior head converges
    to the new schema idempotently.
    """
    import asyncio
    import os
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

    def has_link_table(db_path: str) -> bool:
        import sqlite3

        with sqlite3.connect(db_path) as conn:
            row = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='card_run_link'"
            ).fetchone()
        return row is not None

    with tempfile.TemporaryDirectory() as tmp:
        db_path = os.path.join(tmp, "legacy.db")
        url = f"sqlite+aiosqlite:///{db_path}"

        async def create_at_head() -> None:
            db = await Database.create(Settings(database_url=url))
            await db.close()

        asyncio.run(create_at_head())
        assert has_link_table(db_path)

        # Simulate an existing deployment at the previous head.
        run_alembic("downgrade", "20260826_02", url)
        assert not has_link_table(db_path)

        # The M7.4 migration restores the table exactly once.
        run_alembic("upgrade", "head", url)
        assert has_link_table(db_path)


class _FakeManager:
    """Minimal stand-in for AgentConnectionManager.send_coding_run."""

    async def send_coding_run(self, *args, **kwargs) -> None:
        return None


async def test_run_outcome_annotates_linked_card_idempotently(app):
    """A succeeded capture run annotates its linked card; no duplicate rows."""
    from crewspace.application.change_sets import ChangeSetService
    from crewspace.application.coding_runs import dispatch_coding_run
    from tests.test_change_set_management import _make_cs

    now = dt.datetime.now(dt.timezone.utc)
    run_id = "run_m74_outcome"
    async with app.state.db.uow() as uow:
        card = await uow.boards.add_card(
            "col_todo", "Outcome card", actor_id="user_bilal"
        )
        await uow.coding_repositories.create(
            CodingRepository(
                id="repo_m74_outcome",
                name="Outcome Repo",
                default_branch="main",
                created_by="user_bilal",
                created_at=now,
            )
        )
        await uow.coding_repositories.grant_team(
            TeamRepositoryAccess(
                team_id="team_acme",
                repository_id="repo_m74_outcome",
                granted_by="user_bilal",
                granted_at=now,
            )
        )
        await uow.commit()
    async with app.state.db.uow() as uow:
        run = await dispatch_coding_run(
            uow,
            agent_id="agent_planner",
            team_id="team_acme",
            repository_id="repo_m74_outcome",
            run_id=run_id,
            instruction="work",
            requested_by="user_bilal",
            agent_manager=_FakeManager(),
        )
    request_id = run.request_id

    service = BoardService(build_registry(), app.state.settings)
    user = None
    async with app.state.db.uow() as uow:
        user = await uow.auth.get_member("user_bilal")
    assert user is not None
    async with app.state.db.uow() as uow:
        await service.link_card_to_run(card.id, run_id, user, uow)
        await uow.commit()

    # Capture the run's change set (the real outcome path).
    async with app.state.db.uow() as uow:
        await ChangeSetService().record_capture(
            agent_id="agent_planner",
            request_id=request_id,
            change_set=_make_cs(run_id, "repo_m74_outcome"),
            uow=uow,
        )
        await uow.commit()

    async with app.state.db.uow() as uow:
        statuses = await service.card_run_status(card.id, user, uow)
    assert len(statuses) == 1
    assert statuses[0].run_id == run_id
    assert statuses[0].run_status == "succeeded"
    assert statuses[0].change_set_id is not None
    assert statuses[0].change_set_status == "captured"

    # Re-running the same projection stays idempotent: ONE link, ONE status.
    async with app.state.db.uow() as uow:
        statuses2 = await service.card_run_status(card.id, user, uow)
    assert len(statuses2) == 1


async def test_board_run_statuses_badges_and_deep_links(app):
    """The board view exposes per-card badges with canonical deep links."""
    from crewspace.dto.board import card_run_badges

    card, user = await _seed_card_and_run(app, run_id="run_badge")
    service = BoardService(build_registry(), app.state.settings)

    async with app.state.db.uow() as uow:
        await service.link_card_to_run(card.id, "run_badge", user, uow)
        await uow.commit()

    async with app.state.db.uow() as uow:
        status_map = await service.board_run_statuses("board_main", user, uow)
    assert card.id in status_map
    status = status_map[card.id][0]
    assert status.run_status == "running"
    badges = card_run_badges(status)
    assert badges[0]["href"] == f"/api/coding/runs/{status.run_id}"
    # No change set yet -> no change-set/review badges.
    assert len(badges) == 1


async def test_spawn_coding_run_from_card_tool_links_run(app, monkeypatch):
    """The spawn-run-from-card tool dispatches AND links the new run."""
    from crewspace.api.connection import agent_manager
    from crewspace.application.tools import build_registry

    async def accept_dispatch(*args, **kwargs) -> None:
        return None

    monkeypatch.setattr(agent_manager, "send_coding_run", accept_dispatch)

    now = dt.datetime.now(dt.timezone.utc)
    async with app.state.db.uow() as uow:
        card = await uow.boards.add_card(
            "col_todo", "Spawn card", actor_id="user_bilal"
        )
        await uow.coding_repositories.create(
            CodingRepository(
                id="repo_spawn",
                name="Spawn Repo",
                default_branch="main",
                created_by="user_bilal",
                created_at=now,
            )
        )
        await uow.coding_repositories.grant_team(
            TeamRepositoryAccess(
                team_id="team_acme",
                repository_id="repo_spawn",
                granted_by="user_bilal",
                granted_at=now,
            )
        )
        await uow.commit()

    service = BoardService(build_registry(), app.state.settings)
    user = None
    async with app.state.db.uow() as uow:
        user = await uow.auth.get_member("user_bilal")
    assert user is not None

    reg = build_registry()
    async with app.state.db.uow() as uow:
        runner = reg.bind(
            uow,
            principal_id="user_bilal",
            agent_id="agent_planner",
            allowed_tools={"spawn_coding_run_from_card"},
        )
        result = await runner.run(
            "spawn_coding_run_from_card",
            **{
                "card_id": card.id,
                "agent_id": "agent_planner",
                "repository_id": "repo_spawn",
                "instruction": "Implement the spawn tool",
            },
        )
        await uow.commit()

    assert result["card_id"] == card.id
    assert result["status"] == "running"
    run_id = result["run_id"]
    async with app.state.db.uow() as uow:
        statuses = await service.card_run_status(card.id, user, uow)
    assert any(s.run_id == run_id for s in statuses)
    assert statuses[0].run_status == "running"
