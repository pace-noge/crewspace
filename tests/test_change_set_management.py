"""Team-scoped persistence and governance for remote coding change sets."""
from __future__ import annotations

import asyncio
import datetime as dt

import pytest

from crewspace.api.routers.agents import (
    _handle_coding_change_set,
    _persist_coding_change_set,
)
from crewspace.application.coding_runs import dispatch_coding_run
from crewspace.application.change_sets import ChangeSetService
from crewspace.domain.entities import CodingRepository, CodingRun, TeamRepositoryAccess
from crewspace.dto.change_sets import ChangeSetDTO


def test_coding_run_lifecycle_migration_preserves_legacy_captured_row(tmp_path):
    import sqlite3

    from alembic import command
    from alembic.config import Config

    db_path = tmp_path / "legacy-coding-run.db"
    config = Config("alembic.ini")
    config.set_main_option("sqlalchemy.url", f"sqlite+aiosqlite:///{db_path}")
    command.upgrade(config, "head")
    command.downgrade(config, "20260824_02")
    created_at = "2026-08-24T12:00:00+00:00"
    with sqlite3.connect(db_path) as conn:
        conn.execute("PRAGMA foreign_keys=OFF")
        conn.execute(
            "INSERT INTO coding_run "
            "(id, team_id, repository_id, requested_by, agent_id, request_id, "
            "instruction, status, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "run_legacy_captured",
                "team_legacy",
                "repo_legacy",
                "user_legacy",
                "agent_legacy",
                "request_legacy",
                "Legacy capture",
                "captured",
                created_at,
            ),
        )
        conn.commit()

    command.upgrade(config, "head")

    with sqlite3.connect(db_path) as conn:
        row = conn.execute(
            "SELECT status, created_at, updated_at, started_at, finished_at "
            "FROM coding_run WHERE id='run_legacy_captured'"
        ).fetchone()
    assert row == ("succeeded", created_at, created_at, None, created_at)


def test_coding_run_lifecycle_downgrade_maps_new_terminal_states(tmp_path):
    import sqlite3

    from alembic import command
    from alembic.config import Config

    db_path = tmp_path / "new-coding-runs.db"
    config = Config("alembic.ini")
    config.set_main_option("sqlalchemy.url", f"sqlite+aiosqlite:///{db_path}")
    command.upgrade(config, "head")
    created_at = "2026-08-24T12:00:00+00:00"
    with sqlite3.connect(db_path) as conn:
        conn.execute("PRAGMA foreign_keys=OFF")
        for status in ("queued", "succeeded", "cancelled", "timed_out", "interrupted"):
            conn.execute(
                "INSERT INTO coding_run "
                "(id, team_id, repository_id, requested_by, agent_id, request_id, "
                "instruction, status, created_at, updated_at, started_at, finished_at, recent_output) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    f"run_{status}",
                    "team_legacy",
                    "repo_legacy",
                    "user_legacy",
                    "agent_legacy",
                    f"request_{status}",
                    "Downgrade lifecycle",
                    status,
                    created_at,
                    created_at,
                    None,
                    created_at,
                    "",
                ),
            )
        conn.commit()

    command.downgrade(config, "20260824_02")

    with sqlite3.connect(db_path) as conn:
        rows = dict(conn.execute("SELECT id, status FROM coding_run").fetchall())
        columns = {row[1] for row in conn.execute("PRAGMA table_info(coding_run)")}
    assert rows == {
        "run_queued": "running",
        "run_succeeded": "captured",
        "run_cancelled": "failed",
        "run_timed_out": "failed",
        "run_interrupted": "running",
    }
    assert {"updated_at", "started_at", "finished_at"}.isdisjoint(columns)


@pytest.mark.asyncio
async def test_coding_run_compare_and_set_tracks_lifecycle_timestamps(app):
    now = dt.datetime.now(dt.timezone.utc)
    started = now + dt.timedelta(seconds=1)
    finished = started + dt.timedelta(seconds=2)
    async with app.state.db.uow() as uow:
        await uow.coding_repositories.create(
            CodingRepository(
                id="repo_run_lifecycle",
                name="Run lifecycle",
                default_branch="master",
                created_by="user_bilal",
                created_at=now,
            )
        )
        await uow.coding_repositories.grant_team(
            TeamRepositoryAccess(
                team_id="team_acme",
                repository_id="repo_run_lifecycle",
                granted_by="user_bilal",
                granted_at=now,
            )
        )
        await uow.coding_runs.create(
            CodingRun(
                id="run_lifecycle",
                team_id="team_acme",
                repository_id="repo_run_lifecycle",
                requested_by="user_bilal",
                agent_id="agent_planner",
                request_id="request_lifecycle",
                instruction="Track lifecycle",
                status="queued",
                created_at=now,
                updated_at=now,
                started_at=None,
                finished_at=None,
            )
        )
        assert await uow.coding_runs.transition(
            "run_lifecycle",
            expected="queued",
            status="running",
            updated_at=started,
            started_at=started,
            finished_at=None,
        ) is True
        assert await uow.coding_runs.transition(
            "run_lifecycle",
            expected="queued",
            status="cancelled",
            updated_at=finished,
            started_at=None,
            finished_at=finished,
        ) is False
        assert await uow.coding_runs.transition(
            "run_lifecycle",
            expected="running",
            status="succeeded",
            updated_at=finished,
            started_at=None,
            finished_at=finished,
        ) is True
        await uow.commit()

    async with app.state.db.uow() as uow:
        run = await uow.coding_runs.get("run_lifecycle")
    assert run is not None
    assert run.status == "succeeded"
    assert run.created_at == now
    assert run.updated_at == finished
    assert run.started_at == started
    assert run.finished_at == finished


@pytest.mark.asyncio
async def test_coding_run_create_normalizes_and_returns_consistent_timestamps(app):
    now = dt.datetime.now(dt.timezone.utc)
    async with app.state.db.uow() as uow:
        await uow.coding_repositories.create(
            CodingRepository(
                id="repo_create_norm",
                name="Create normalization",
                default_branch="master",
                created_by="user_bilal",
                created_at=now,
            )
        )
        await uow.coding_repositories.grant_team(
            TeamRepositoryAccess(
                team_id="team_acme",
                repository_id="repo_create_norm",
                granted_by="user_bilal",
                granted_at=now,
            )
        )
        created = await uow.coding_runs.create(
            CodingRun(
                id="run_create_norm",
                team_id="team_acme",
                repository_id="repo_create_norm",
                requested_by="user_bilal",
                agent_id="agent_planner",
                request_id="request_create_norm",
                instruction="Normalize on create",
                status="queued",
                created_at=now,
                updated_at=None,
                started_at=now,
                finished_at=None,
            )
        )
        await uow.commit()

    assert created.updated_at == now
    assert created.started_at is None
    assert created.finished_at is None
    async with app.state.db.uow() as uow:
        stored = await uow.coding_runs.get("run_create_norm")
    assert stored is not None
    assert stored.status == "queued"
    assert stored.updated_at == now
    assert stored.started_at is None
    assert stored.finished_at is None


@pytest.mark.asyncio
async def test_coding_run_rejects_invalid_lifecycle_edge(app):
    now = dt.datetime.now(dt.timezone.utc)
    async with app.state.db.uow() as uow:
        await uow.coding_repositories.create(
            CodingRepository(
                id="repo_invalid_edge",
                name="Invalid edge",
                default_branch="master",
                created_by="user_bilal",
                created_at=now,
            )
        )
        await uow.coding_repositories.grant_team(
            TeamRepositoryAccess(
                team_id="team_acme",
                repository_id="repo_invalid_edge",
                granted_by="user_bilal",
                granted_at=now,
            )
        )
        await uow.coding_runs.create(
            CodingRun(
                id="run_invalid_edge",
                team_id="team_acme",
                repository_id="repo_invalid_edge",
                requested_by="user_bilal",
                agent_id="agent_planner",
                request_id="request_invalid_edge",
                instruction="Reject invalid edge",
                status="queued",
                created_at=now,
                updated_at=now,
            )
        )
        with pytest.raises(ValueError, match="Invalid coding-run transition"):
            await uow.coding_runs.transition(
                "run_invalid_edge",
                expected="queued",
                status="succeeded",
                updated_at=now,
                started_at=None,
                finished_at=now,
            )
        await uow.commit()

    async with app.state.db.uow() as uow:
        run = await uow.coding_runs.get("run_invalid_edge")
    assert run is not None
    assert run.status == "queued"
    assert run.started_at is None
    assert run.finished_at is None


@pytest.mark.asyncio
@pytest.mark.parametrize("terminal_status", ["failed", "cancelled", "timed_out", "interrupted"])
async def test_coding_run_accepts_each_non_success_terminal_status(app, terminal_status):
    now = dt.datetime.now(dt.timezone.utc)
    run_id = f"run_terminal_{terminal_status}"
    async with app.state.db.uow() as uow:
        await uow.coding_repositories.create(
            CodingRepository(
                id=f"repo_terminal_{terminal_status}",
                name=f"Terminal {terminal_status}",
                default_branch="master",
                created_by="user_bilal",
                created_at=now,
            )
        )
        await uow.coding_repositories.grant_team(
            TeamRepositoryAccess(
                team_id="team_acme",
                repository_id=f"repo_terminal_{terminal_status}",
                granted_by="user_bilal",
                granted_at=now,
            )
        )
        await uow.coding_runs.create(
            CodingRun(
                id=run_id,
                team_id="team_acme",
                repository_id=f"repo_terminal_{terminal_status}",
                requested_by="user_bilal",
                agent_id="agent_planner",
                request_id=f"request_terminal_{terminal_status}",
                instruction="Reach terminal state",
                status="running",
                created_at=now,
                updated_at=now,
            )
        )
        assert await uow.coding_runs.transition(
            run_id,
            expected="running",
            status=terminal_status,
            updated_at=now,
            started_at=None,
            finished_at=now,
        ) is True
        await uow.commit()

    async with app.state.db.uow() as uow:
        run = await uow.coding_runs.get(run_id)
    assert run is not None
    assert run.status == terminal_status
    assert run.started_at is None
    assert run.finished_at == now


@pytest.mark.asyncio
async def test_coding_change_set_handler_persists_before_completing_waiter(app):
    class Manager:
        completed = False
        failed = False

        def validate_coding_change_set(self, agent_id, request_id, value):
            assert not self.completed
            return ChangeSetDTO.model_validate(value)

        def complete_coding_change_set(self, agent_id, request_id, change_set):
            self.completed = True
            return True

        def deliver_coding_failure(self, agent_id, request_id, error):
            self.failed = True
            return True

    now = dt.datetime.now(dt.timezone.utc)
    async with app.state.db.uow() as uow:
        await uow.coding_repositories.create(CodingRepository(
            id="repo_handler", name="Handler", default_branch="master",
            created_by="user_bilal", created_at=now,
        ))
        await uow.coding_repositories.grant_team(TeamRepositoryAccess(
            team_id="team_acme", repository_id="repo_handler",
            granted_by="user_bilal", granted_at=now,
        ))
        await uow.coding_runs.create(CodingRun(
            id="run_handler", team_id="team_acme", repository_id="repo_handler",
            requested_by="user_bilal", agent_id="agent_planner",
            request_id="request_handler", instruction="Handle ingress",
            status="running", created_at=now,
                updated_at=now,
        ))
        await uow.commit()

    manager = Manager()
    error = await _handle_coding_change_set(
        manager,
        app.state.db,
        agent_id="agent_planner",
        request_id="request_handler",
        value={
            "repository_id": "repo_handler", "run_id": "run_handler",
            "branch": "crewspace/run_handler", "base_commit": "a" * 40,
            "head_commit": "b" * 40, "commits": [], "files": [],
            "additions": 0, "deletions": 0, "verification": [], "artifacts": [],
        },
    )

    assert error is None
    assert manager.completed is True
    assert manager.failed is False
    async with app.state.db.uow() as uow:
        records = await uow.change_sets.list_for_teams(["team_acme"])
        assert len(records) == 1
        assert records[0].run_id == "run_handler"


@pytest.mark.asyncio
async def test_coding_change_set_handler_fails_waiter_when_persistence_rejects(app):
    class Manager:
        completed = False
        failed_error = None

        def validate_coding_change_set(self, agent_id, request_id, value):
            return ChangeSetDTO.model_validate(value)

        def complete_coding_change_set(self, agent_id, request_id, change_set):
            self.completed = True
            return True

        def deliver_coding_failure(self, agent_id, request_id, error):
            self.failed_error = error
            return True

    manager = Manager()
    error = await _handle_coding_change_set(
        manager,
        app.state.db,
        agent_id="agent_planner",
        request_id="request_missing",
        value={
            "repository_id": "repo_missing", "run_id": "run_missing",
            "branch": "crewspace/run_missing", "base_commit": "a" * 40,
            "head_commit": "b" * 40, "commits": [], "files": [],
            "additions": 0, "deletions": 0, "verification": [], "artifacts": [],
        },
    )

    assert error == "change set persistence failed"
    assert manager.failed_error == "change set persistence failed"
    assert manager.completed is False


@pytest.mark.asyncio
async def test_authenticated_coding_result_ingress_persists_bound_change_set(app):
    now = dt.datetime.now(dt.timezone.utc)
    async with app.state.db.uow() as uow:
        await uow.coding_repositories.create(CodingRepository(
            id="repo_ingress", name="Ingress", default_branch="master",
            created_by="user_bilal", created_at=now,
        ))
        await uow.coding_repositories.grant_team(TeamRepositoryAccess(
            team_id="team_acme", repository_id="repo_ingress",
            granted_by="user_bilal", granted_at=now,
        ))
        await uow.coding_runs.create(CodingRun(
            id="run_ingress", team_id="team_acme", repository_id="repo_ingress",
            requested_by="user_bilal", agent_id="agent_planner",
            request_id="request_ingress", instruction="Persist ingress",
            status="running", created_at=now,
                updated_at=now,
        ))
        await uow.commit()

    change_set = ChangeSetDTO.model_validate({
        "repository_id": "repo_ingress", "run_id": "run_ingress",
        "branch": "crewspace/run_ingress", "base_commit": "a" * 40,
        "head_commit": "b" * 40, "commits": [], "files": [],
        "additions": 0, "deletions": 0, "verification": [], "artifacts": [],
    })
    await _persist_coding_change_set(
        app.state.db,
        agent_id="agent_planner",
        request_id="request_ingress",
        change_set=change_set,
    )

    async with app.state.db.uow() as uow:
        records = await uow.change_sets.list_for_teams(["team_acme"])
        assert len(records) == 1
        assert records[0].run_id == "run_ingress"
        run = await uow.coding_runs.get("run_ingress")
        assert run is not None
        assert run.status == "succeeded"
        assert run.started_at is None
        assert run.finished_at is not None
        assert run.updated_at == run.finished_at
        audit = await uow.change_sets.list_audit(records[0].id)
        assert [event.action for event in audit] == ["captured"]


@pytest.mark.asyncio
async def test_validated_remote_change_set_is_persisted_for_bound_team_run(app):
    now = dt.datetime.now(dt.timezone.utc)
    change_set = ChangeSetDTO.model_validate(
        {
            "repository_id": "repo_crewspace",
            "run_id": "run_123",
            "branch": "crewspace/run_123",
            "base_commit": "a" * 40,
            "head_commit": "b" * 40,
            "commits": [{"sha": "b" * 40, "subject": "Implement feature"}],
            "files": [
                {
                    "path": "src/crewspace/main.py",
                    "status": "modified",
                    "additions": 4,
                    "deletions": 1,
                }
            ],
            "additions": 4,
            "deletions": 1,
            "verification": [
                {"name": "pytest", "status": "passed", "summary": "1 passed"}
            ],
            "artifacts": [{"path": "reports/test.xml", "size_bytes": 128}],
        }
    )

    async with app.state.db.uow() as uow:
        await uow.coding_repositories.create(
            CodingRepository(
                id="repo_crewspace",
                name="Crewspace",
                default_branch="master",
                created_by="user_bilal",
                created_at=now,
            )
        )
        await uow.coding_repositories.grant_team(
            TeamRepositoryAccess(
                team_id="team_acme",
                repository_id="repo_crewspace",
                granted_by="user_bilal",
                granted_at=now,
            )
        )
        await uow.coding_runs.create(
            CodingRun(
                id="run_123",
                team_id="team_acme",
                repository_id="repo_crewspace",
                requested_by="user_bilal",
                agent_id="agent_planner",
                request_id="request_123",
                instruction="Implement the requested feature",
                status="running",
                created_at=now,
                updated_at=now,
            )
        )

        stored = await ChangeSetService().record_capture(
            agent_id="agent_planner",
            request_id="request_123",
            change_set=change_set,
            uow=uow,
        )
        await uow.commit()

    async with app.state.db.uow() as uow:
        persisted = await uow.change_sets.get(stored.id)
        audit = await uow.change_sets.list_audit(stored.id)

    assert persisted is not None
    assert persisted.team_id == "team_acme"
    assert persisted.repository_id == "repo_crewspace"
    assert persisted.run_id == "run_123"
    assert persisted.agent_id == "agent_planner"
    assert persisted.request_id == "request_123"
    assert persisted.status == "captured"
    assert persisted.payload["files"][0]["path"] == "src/crewspace/main.py"
    assert "workspace_path" not in persisted.payload
    assert [(event.action, event.actor_id) for event in audit] == [
        ("captured", "agent_planner")
    ]


def test_change_set_detail_uses_app_shell_and_renders_path_free_metadata(app):
    import asyncio

    from starlette.testclient import TestClient

    async def arrange() -> str:
        now = dt.datetime.now(dt.timezone.utc)
        change_set = ChangeSetDTO.model_validate(
            {
                "repository_id": "repo_crewspace",
                "run_id": "run_detail",
                "branch": "crewspace/run_detail",
                "base_commit": "a" * 40,
                "head_commit": "b" * 40,
                "commits": [{"sha": "b" * 40, "subject": "Render details"}],
                "files": [{
                    "path": "src/crewspace/main.py", "status": "modified",
                    "additions": 4, "deletions": 1,
                }],
                "additions": 4,
                "deletions": 1,
                "verification": [{
                    "name": "pytest", "status": "passed", "summary": "1 passed",
                }],
                "artifacts": [{"path": "reports/test.xml", "size_bytes": 128}],
            }
        )
        async with app.state.db.uow() as uow:
            await uow.coding_repositories.create(CodingRepository(
                id="repo_crewspace", name="Crewspace", default_branch="master",
                created_by="user_bilal", created_at=now,
            ))
            await uow.coding_repositories.grant_team(TeamRepositoryAccess(
                team_id="team_acme", repository_id="repo_crewspace",
                granted_by="user_bilal", granted_at=now,
            ))
            await uow.coding_runs.create(CodingRun(
                id="run_detail", team_id="team_acme", repository_id="repo_crewspace",
                requested_by="user_bilal", agent_id="agent_planner",
                request_id="request_detail", instruction="Render change-set details",
                status="running", created_at=now,
                updated_at=now,
            ))
            stored = await ChangeSetService().record_capture(
                agent_id="agent_planner", request_id="request_detail",
                change_set=change_set, uow=uow,
            )
            await uow.commit()
            return stored.id

    change_set_id = asyncio.run(arrange())
    with TestClient(app) as client:
        client.headers["Origin"] = "http://testserver"
        assert client.post(
            "/auth/login", data={"username": "Bilal", "password": "admin123"}
        ).status_code == 200
        response = client.get(f"/management/change-sets/{change_set_id}")

    assert response.status_code == 200
    assert 'class="sidebar"' in response.text
    assert "Change set details" in response.text
    assert "Render details" in response.text
    assert "src/crewspace/main.py" in response.text
    assert "+4" in response.text and "−1" in response.text
    assert "pytest" in response.text and "1 passed" in response.text
    assert "reports/test.xml" in response.text
    assert "Captured" in response.text and "agent_planner" in response.text
    assert "workspace_path" not in response.text


def test_change_set_detail_rejects_user_without_team_management_scope(app):
    import asyncio

    from starlette.testclient import TestClient

    async def arrange() -> str:
        now = dt.datetime.now(dt.timezone.utc)
        change_set = ChangeSetDTO.model_validate(
            {
                "repository_id": "repo_scope",
                "run_id": "run_scope",
                "branch": "crewspace/run_scope",
                "base_commit": "a" * 40,
                "head_commit": "b" * 40,
                "commits": [], "files": [], "additions": 0, "deletions": 0,
                "verification": [], "artifacts": [],
            }
        )
        async with app.state.db.uow() as uow:
            await uow.coding_repositories.create(CodingRepository(
                id="repo_scope", name="Scoped", default_branch="master",
                created_by="user_bilal", created_at=now,
            ))
            await uow.coding_repositories.grant_team(TeamRepositoryAccess(
                team_id="team_acme", repository_id="repo_scope",
                granted_by="user_bilal", granted_at=now,
            ))
            await uow.coding_runs.create(CodingRun(
                id="run_scope", team_id="team_acme", repository_id="repo_scope",
                requested_by="user_bilal", agent_id="agent_planner",
                request_id="request_scope", instruction="Scoped change",
                status="running", created_at=now,
                updated_at=now,
            ))
            stored = await ChangeSetService().record_capture(
                agent_id="agent_planner", request_id="request_scope",
                change_set=change_set, uow=uow,
            )
            await uow._conn.execute(
                "UPDATE member SET role='team_member' WHERE id='user_bilal'"
            )
            await uow._conn.execute(
                "UPDATE team_member SET role='member' "
                "WHERE team_id='team_acme' AND member_id='user_bilal'"
            )
            await uow.commit()
            return stored.id

    change_set_id = asyncio.run(arrange())
    with TestClient(app) as client:
        client.headers["Origin"] = "http://testserver"
        assert client.post(
            "/auth/login", data={"username": "Bilal", "password": "admin123"}
        ).status_code == 200
        response = client.get(f"/management/change-sets/{change_set_id}")

    assert response.status_code == 403


def test_team_manager_can_review_captured_change_set_once(app):
    import asyncio

    from starlette.testclient import TestClient

    async def arrange() -> str:
        now = dt.datetime.now(dt.timezone.utc)
        change_set = ChangeSetDTO.model_validate(
            {
                "repository_id": "repo_review",
                "run_id": "run_review",
                "branch": "crewspace/run_review",
                "base_commit": "a" * 40,
                "head_commit": "b" * 40,
                "commits": [], "files": [], "additions": 0, "deletions": 0,
                "verification": [], "artifacts": [],
            }
        )
        async with app.state.db.uow() as uow:
            await uow.coding_repositories.create(CodingRepository(
                id="repo_review", name="Review", default_branch="master",
                created_by="user_bilal", created_at=now,
            ))
            await uow.coding_repositories.grant_team(TeamRepositoryAccess(
                team_id="team_acme", repository_id="repo_review",
                granted_by="user_bilal", granted_at=now,
            ))
            await uow.coding_runs.create(CodingRun(
                id="run_review", team_id="team_acme", repository_id="repo_review",
                requested_by="user_bilal", agent_id="agent_planner",
                request_id="request_review", instruction="Review me",
                status="running", created_at=now,
                updated_at=now,
            ))
            stored = await ChangeSetService().record_capture(
                agent_id="agent_planner", request_id="request_review",
                change_set=change_set, uow=uow,
            )
            await uow.commit()
            return stored.id

    change_set_id = asyncio.run(arrange())
    with TestClient(app) as client:
        client.headers["Origin"] = "http://testserver"
        assert client.post(
            "/auth/login", data={"username": "Bilal", "password": "admin123"}
        ).status_code == 200
        reviewed = client.post(
            f"/management/change-sets/{change_set_id}/review",
            follow_redirects=False,
        )
        repeated = client.post(
            f"/management/change-sets/{change_set_id}/review",
            follow_redirects=False,
        )

    assert reviewed.status_code == 303
    assert reviewed.headers["location"] == f"/management/change-sets/{change_set_id}"
    assert repeated.status_code == 409

    async def read_back():
        async with app.state.db.uow() as uow:
            return (
                await uow.change_sets.get(change_set_id),
                await uow.change_sets.list_audit(change_set_id),
            )

    stored, audit = asyncio.run(read_back())
    assert stored is not None and stored.status == "reviewed"
    assert [(event.action, event.actor_id) for event in audit] == [
        ("captured", "agent_planner"),
        ("reviewed", "user_bilal"),
    ]


def test_captured_detail_offers_governed_review_action(app):
    import asyncio

    from starlette.testclient import TestClient

    async def arrange() -> str:
        now = dt.datetime.now(dt.timezone.utc)
        change_set = ChangeSetDTO.model_validate({
            "repository_id": "repo_button", "run_id": "run_button",
            "branch": "crewspace/run_button", "base_commit": "a" * 40,
            "head_commit": "b" * 40, "commits": [], "files": [],
            "additions": 0, "deletions": 0, "verification": [], "artifacts": [],
        })
        async with app.state.db.uow() as uow:
            await uow.coding_repositories.create(CodingRepository(
                id="repo_button", name="Button", default_branch="master",
                created_by="user_bilal", created_at=now,
            ))
            await uow.coding_repositories.grant_team(TeamRepositoryAccess(
                team_id="team_acme", repository_id="repo_button",
                granted_by="user_bilal", granted_at=now,
            ))
            await uow.coding_runs.create(CodingRun(
                id="run_button", team_id="team_acme", repository_id="repo_button",
                requested_by="user_bilal", agent_id="agent_planner",
                request_id="request_button", instruction="Show action",
                status="running", created_at=now,
                updated_at=now,
            ))
            stored = await ChangeSetService().record_capture(
                agent_id="agent_planner", request_id="request_button",
                change_set=change_set, uow=uow,
            )
            await uow.commit()
            return stored.id

    change_set_id = asyncio.run(arrange())
    with TestClient(app) as client:
        client.headers["Origin"] = "http://testserver"
        assert client.post(
            "/auth/login", data={"username": "Bilal", "password": "admin123"}
        ).status_code == 200
        before = client.get(f"/management/change-sets/{change_set_id}")
        assert f'href="/management/change-sets/{change_set_id}/review"' in before.text
        assert ">Mark reviewed<" in before.text
        assert "Request PR" not in before.text
        review_page = client.get(f"/management/change-sets/{change_set_id}/review")
        assert review_page.status_code == 200
        assert 'class="sidebar"' in review_page.text
        assert "Review change set" in review_page.text
        assert f'action="/management/change-sets/{change_set_id}/review"' in review_page.text
        assert ">Mark reviewed<" in review_page.text
        assert f'href="/management/change-sets/{change_set_id}"' in review_page.text
        client.post(f"/management/change-sets/{change_set_id}/review")
        after = client.get(f"/management/change-sets/{change_set_id}")

    assert ">Mark reviewed<" not in after.text
    assert f'href="/management/change-sets/{change_set_id}/request-pr"' in after.text
    assert f'href="/management/change-sets/{change_set_id}/retain"' in after.text
    assert f'href="/management/change-sets/{change_set_id}/request-discard"' in after.text

    expected = {
        "request-pr": ("Request PR", "Request pull request"),
        "retain": ("Retain workspace", "Retain workspace"),
        "request-discard": ("Request discard", "Request discard"),
    }
    with TestClient(app) as client:
        client.headers["Origin"] = "http://testserver"
        assert client.post(
            "/auth/login", data={"username": "Bilal", "password": "admin123"}
        ).status_code == 200
        for endpoint, (heading, submit_label) in expected.items():
            page = client.get(
                f"/management/change-sets/{change_set_id}/{endpoint}"
            )
            assert page.status_code == 200
            assert 'class="sidebar"' in page.text
            assert heading in page.text
            assert (
                f'action="/management/change-sets/{change_set_id}/{endpoint}"'
                in page.text
            )
            assert f">{submit_label}<" in page.text
            assert f'href="/management/change-sets/{change_set_id}"' in page.text


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("decision", "remote_action", "remote_status", "final_status", "final_audit"),
    [
        ("retain", "retain", "retained", "retained", "workspace_retained"),
        (
            "request_discard",
            "discard",
            "removed",
            "discarded",
            "workspace_discarded",
        ),
    ],
)
async def test_workspace_decision_commits_intent_then_remote_result(
    app,
    decision: str,
    remote_action: str,
    remote_status: str,
    final_status: str,
    final_audit: str,
):
    from crewspace.application.change_sets import execute_workspace_decision

    now = dt.datetime.now(dt.timezone.utc)
    repository_id = f"repo_remote_{remote_action}"
    run_id = f"run_remote_{remote_action}"
    request_id = f"request_remote_{remote_action}"
    branch = f"crewspace/{run_id}"
    async with app.state.db.uow() as uow:
        await uow.coding_repositories.create(CodingRepository(
            id=repository_id, name=remote_action, default_branch="master",
            created_by="user_bilal", created_at=now,
        ))
        await uow.coding_repositories.grant_team(TeamRepositoryAccess(
            team_id="team_acme", repository_id=repository_id,
            granted_by="user_bilal", granted_at=now,
        ))
        await uow.coding_runs.create(CodingRun(
            id=run_id, team_id="team_acme", repository_id=repository_id,
            requested_by="user_bilal", agent_id="agent_planner",
            request_id=request_id, instruction="Remote lifecycle",
            status="running", created_at=now,
                updated_at=now,
        ))
        stored = await ChangeSetService().record_capture(
            agent_id="agent_planner", request_id=request_id,
            change_set=ChangeSetDTO.model_validate({
                "repository_id": repository_id, "run_id": run_id,
                "branch": branch, "base_commit": "a" * 40,
                "head_commit": "b" * 40, "commits": [], "files": [],
                "additions": 0, "deletions": 0,
                "verification": [], "artifacts": [],
            }),
            uow=uow,
        )
        await ChangeSetService().review(
            change_set_id=stored.id, actor_id="user_bilal", uow=uow
        )
        await uow.commit()

    class RemoteManager:
        async def send_workspace_action(self, agent_id: str, **kwargs):
            assert agent_id == "agent_planner"
            assert kwargs == {
                "repository_id": repository_id,
                "run_id": run_id,
                "branch": branch,
                "action": remote_action,
                "timeout": 30.0,
            }
            async with app.state.db.uow() as check_uow:
                pending = await check_uow.change_sets.get(stored.id)
                assert pending is not None
                assert pending.status == f"{remote_action}_requested"
            return {
                "repository_id": repository_id,
                "run_id": run_id,
                "branch": branch,
                "action": remote_action,
                "status": remote_status,
            }

    result = await execute_workspace_decision(
        db=app.state.db,
        manager=RemoteManager(),
        change_set_id=stored.id,
        decision=decision,
        current_user={"id": "user_bilal", "role": "superadmin"},
        timeout=30.0,
    )

    assert result.status == final_status
    async with app.state.db.uow() as uow:
        audit = await uow.change_sets.list_audit(stored.id)
    assert [(event.action, event.actor_id) for event in audit][-2:] == [
        (f"{remote_action}_requested", "user_bilal"),
        (final_audit, "agent_planner"),
    ]


@pytest.mark.asyncio
async def test_workspace_decision_failure_returns_to_reviewed_for_retry(app):
    from crewspace.application.change_sets import execute_workspace_decision

    now = dt.datetime.now(dt.timezone.utc)
    change_set = ChangeSetDTO.model_validate({
        "repository_id": "repo_retry", "run_id": "run_retry",
        "branch": "crewspace/run_retry", "base_commit": "a" * 40,
        "head_commit": "b" * 40, "commits": [], "files": [],
        "additions": 0, "deletions": 0, "verification": [], "artifacts": [],
    })
    async with app.state.db.uow() as uow:
        await uow.coding_repositories.create(CodingRepository(
            id="repo_retry", name="Retry", default_branch="master",
            created_by="user_bilal", created_at=now,
        ))
        await uow.coding_repositories.grant_team(TeamRepositoryAccess(
            team_id="team_acme", repository_id="repo_retry",
            granted_by="user_bilal", granted_at=now,
        ))
        await uow.coding_runs.create(CodingRun(
            id="run_retry", team_id="team_acme", repository_id="repo_retry",
            requested_by="user_bilal", agent_id="agent_planner",
            request_id="request_retry", instruction="Retry lifecycle",
            status="running", created_at=now,
                updated_at=now,
        ))
        stored = await ChangeSetService().record_capture(
            agent_id="agent_planner", request_id="request_retry",
            change_set=change_set, uow=uow,
        )
        await ChangeSetService().review(
            change_set_id=stored.id, actor_id="user_bilal", uow=uow
        )
        await uow.commit()

    class FailingManager:
        async def send_workspace_action(self, agent_id: str, **kwargs):
            raise TimeoutError("private remote detail must not be persisted")

    with pytest.raises(TimeoutError, match="private remote detail"):
        await execute_workspace_decision(
            db=app.state.db,
            manager=FailingManager(),
            change_set_id=stored.id,
            decision="retain",
            current_user={"id": "user_bilal", "role": "superadmin"},
            timeout=30.0,
        )

    async with app.state.db.uow() as uow:
        retried = await uow.change_sets.get(stored.id)
        audit = await uow.change_sets.list_audit(stored.id)
    assert retried is not None and retried.status == "reviewed"
    assert [(event.action, event.actor_id) for event in audit][-2:] == [
        ("retain_requested", "user_bilal"),
        ("workspace_action_failed", "user_bilal"),
    ]
    assert "private remote detail" not in repr(audit)


def test_retain_route_waits_for_remote_acknowledgement(
    app, monkeypatch: pytest.MonkeyPatch
):
    import asyncio

    from starlette.testclient import TestClient
    from crewspace.api.routers import change_sets as change_set_routes

    async def arrange() -> str:
        now = dt.datetime.now(dt.timezone.utc)
        async with app.state.db.uow() as uow:
            await uow.coding_repositories.create(CodingRepository(
                id="repo_route_retain", name="Route retain", default_branch="master",
                created_by="user_bilal", created_at=now,
            ))
            await uow.coding_repositories.grant_team(TeamRepositoryAccess(
                team_id="team_acme", repository_id="repo_route_retain",
                granted_by="user_bilal", granted_at=now,
            ))
            await uow.coding_runs.create(CodingRun(
                id="run_route_retain", team_id="team_acme",
                repository_id="repo_route_retain", requested_by="user_bilal",
                agent_id="agent_planner", request_id="request_route_retain",
                instruction="Retain remotely", status="running", created_at=now,
                updated_at=now,
            ))
            stored = await ChangeSetService().record_capture(
                agent_id="agent_planner", request_id="request_route_retain",
                change_set=ChangeSetDTO.model_validate({
                    "repository_id": "repo_route_retain",
                    "run_id": "run_route_retain",
                    "branch": "crewspace/run_route_retain",
                    "base_commit": "a" * 40, "head_commit": "b" * 40,
                    "commits": [], "files": [], "additions": 0,
                    "deletions": 0, "verification": [], "artifacts": [],
                }),
                uow=uow,
            )
            await ChangeSetService().review(
                change_set_id=stored.id, actor_id="user_bilal", uow=uow
            )
            await uow.commit()
            return stored.id

    class Manager:
        async def send_workspace_action(self, agent_id: str, **kwargs):
            assert agent_id == "agent_planner"
            assert "path" not in kwargs
            return {
                "repository_id": kwargs["repository_id"],
                "run_id": kwargs["run_id"],
                "branch": kwargs["branch"],
                "action": kwargs["action"],
                "status": "retained",
            }

    change_set_id = asyncio.run(arrange())
    monkeypatch.setattr(change_set_routes, "agent_manager", Manager())
    with TestClient(app) as client:
        client.headers["Origin"] = "http://testserver"
        client.post(
            "/auth/login", data={"username": "Bilal", "password": "admin123"}
        )
        response = client.post(
            f"/management/change-sets/{change_set_id}/retain",
            follow_redirects=False,
        )
    assert response.status_code == 303

    async def read_back():
        async with app.state.db.uow() as uow:
            return await uow.change_sets.get(change_set_id)

    stored = asyncio.run(read_back())
    assert stored is not None and stored.status == "retained"


@pytest.mark.asyncio
async def test_workspace_decision_rechecks_team_authorization_before_intent(app):
    from crewspace.application.change_sets import execute_workspace_decision

    now = dt.datetime.now(dt.timezone.utc)
    change_set = ChangeSetDTO.model_validate({
        "repository_id": "repo_revoked", "run_id": "run_revoked",
        "branch": "crewspace/run_revoked", "base_commit": "a" * 40,
        "head_commit": "b" * 40, "commits": [], "files": [],
        "additions": 0, "deletions": 0, "verification": [], "artifacts": [],
    })
    async with app.state.db.uow() as uow:
        await uow._conn.execute(
            "UPDATE member SET role='engineering_manager' WHERE id='user_bilal'"
        )
        await uow.coding_repositories.create(CodingRepository(
            id="repo_revoked", name="Revoked", default_branch="master",
            created_by="user_bilal", created_at=now,
        ))
        await uow.coding_repositories.grant_team(TeamRepositoryAccess(
            team_id="team_acme", repository_id="repo_revoked",
            granted_by="user_bilal", granted_at=now,
        ))
        await uow.coding_runs.create(CodingRun(
            id="run_revoked", team_id="team_acme", repository_id="repo_revoked",
            requested_by="user_bilal", agent_id="agent_planner",
            request_id="request_revoked", instruction="Do not dispatch",
            status="running", created_at=now,
                updated_at=now,
        ))
        stored = await ChangeSetService().record_capture(
            agent_id="agent_planner", request_id="request_revoked",
            change_set=change_set, uow=uow,
        )
        await ChangeSetService().review(
            change_set_id=stored.id, actor_id="user_bilal", uow=uow
        )
        await uow.teams.remove_member("team_acme", "user_bilal")
        await uow.commit()

    class ForbiddenManager:
        async def send_workspace_action(self, agent_id: str, **kwargs):
            raise AssertionError("remote action must not be dispatched")

    with pytest.raises(PermissionError, match="cannot manage"):
        await execute_workspace_decision(
            db=app.state.db, manager=ForbiddenManager(), change_set_id=stored.id,
            decision="retain",
            current_user={"id": "user_bilal", "role": "engineering_manager"},
            timeout=30.0,
        )

    async with app.state.db.uow() as uow:
        current = await uow.change_sets.get(stored.id)
        audit = await uow.change_sets.list_audit(stored.id)
    assert current is not None and current.status == "reviewed"
    assert audit[-1].action == "reviewed"


@pytest.mark.asyncio
async def test_workspace_decision_cancellation_returns_to_reviewed(app):
    import asyncio

    from crewspace.application.change_sets import execute_workspace_decision

    now = dt.datetime.now(dt.timezone.utc)
    change_set = ChangeSetDTO.model_validate({
        "repository_id": "repo_cancel", "run_id": "run_cancel",
        "branch": "crewspace/run_cancel", "base_commit": "a" * 40,
        "head_commit": "b" * 40, "commits": [], "files": [],
        "additions": 0, "deletions": 0, "verification": [], "artifacts": [],
    })
    async with app.state.db.uow() as uow:
        await uow.coding_repositories.create(CodingRepository(
            id="repo_cancel", name="Cancel", default_branch="master",
            created_by="user_bilal", created_at=now,
        ))
        await uow.coding_repositories.grant_team(TeamRepositoryAccess(
            team_id="team_acme", repository_id="repo_cancel",
            granted_by="user_bilal", granted_at=now,
        ))
        await uow.coding_runs.create(CodingRun(
            id="run_cancel", team_id="team_acme", repository_id="repo_cancel",
            requested_by="user_bilal", agent_id="agent_planner",
            request_id="request_cancel", instruction="Cancel lifecycle",
            status="running", created_at=now,
                updated_at=now,
        ))
        stored = await ChangeSetService().record_capture(
            agent_id="agent_planner", request_id="request_cancel",
            change_set=change_set, uow=uow,
        )
        await ChangeSetService().review(
            change_set_id=stored.id, actor_id="user_bilal", uow=uow
        )
        await uow.commit()

    started = asyncio.Event()

    class WaitingManager:
        async def send_workspace_action(self, agent_id: str, **kwargs):
            started.set()
            await asyncio.Event().wait()

    task = asyncio.create_task(execute_workspace_decision(
        db=app.state.db, manager=WaitingManager(), change_set_id=stored.id,
        decision="retain",
        current_user={"id": "user_bilal", "role": "superadmin"},
        timeout=30.0,
    ))
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    async with app.state.db.uow() as uow:
        current = await uow.change_sets.get(stored.id)
        audit = await uow.change_sets.list_audit(stored.id)
    assert current is not None and current.status == "reviewed"
    assert audit[-1].action == "workspace_action_failed"


@pytest.mark.asyncio
async def test_workspace_decision_rejects_wrong_remote_terminal_status(app):
    from crewspace.application.change_sets import execute_workspace_decision

    now = dt.datetime.now(dt.timezone.utc)
    change_set = ChangeSetDTO.model_validate({
        "repository_id": "repo_wrong_status", "run_id": "run_wrong_status",
        "branch": "crewspace/run_wrong_status", "base_commit": "a" * 40,
        "head_commit": "b" * 40, "commits": [], "files": [],
        "additions": 0, "deletions": 0, "verification": [], "artifacts": [],
    })
    async with app.state.db.uow() as uow:
        await uow.coding_repositories.create(CodingRepository(
            id="repo_wrong_status", name="Wrong status", default_branch="master",
            created_by="user_bilal", created_at=now,
        ))
        await uow.coding_repositories.grant_team(TeamRepositoryAccess(
            team_id="team_acme", repository_id="repo_wrong_status",
            granted_by="user_bilal", granted_at=now,
        ))
        await uow.coding_runs.create(CodingRun(
            id="run_wrong_status", team_id="team_acme",
            repository_id="repo_wrong_status", requested_by="user_bilal",
            agent_id="agent_planner", request_id="request_wrong_status",
            instruction="Reject wrong status", status="running", created_at=now,
                updated_at=now,
        ))
        stored = await ChangeSetService().record_capture(
            agent_id="agent_planner", request_id="request_wrong_status",
            change_set=change_set, uow=uow,
        )
        await ChangeSetService().review(
            change_set_id=stored.id, actor_id="user_bilal", uow=uow
        )
        await uow.commit()

    class WrongStatusManager:
        async def send_workspace_action(self, agent_id: str, **kwargs):
            return {**kwargs, "status": "already_removed"}

    with pytest.raises(ValueError, match="unexpected status"):
        await execute_workspace_decision(
            db=app.state.db,
            manager=WrongStatusManager(),
            change_set_id=stored.id,
            decision="retain",
            current_user={"id": "user_bilal", "role": "superadmin"},
            timeout=30.0,
        )

    async with app.state.db.uow() as uow:
        current = await uow.change_sets.get(stored.id)
    assert current is not None and current.status == "reviewed"


@pytest.mark.parametrize(
    ("endpoint", "status", "action"),
    [("request-pr", "pr_requested", "pr_requested")],
)
def test_reviewed_change_set_accepts_one_governed_decision(
    app, endpoint: str, status: str, action: str
):
    import asyncio

    from starlette.testclient import TestClient

    async def arrange() -> str:
        now = dt.datetime.now(dt.timezone.utc)
        repository_id = f"repo_{endpoint.replace('-', '_')}"
        run_id = f"run_{endpoint.replace('-', '_')}"
        request_id = f"request_{endpoint.replace('-', '_')}"
        change_set = ChangeSetDTO.model_validate({
            "repository_id": repository_id, "run_id": run_id,
            "branch": f"crewspace/{run_id}", "base_commit": "a" * 40,
            "head_commit": "b" * 40, "commits": [], "files": [],
            "additions": 0, "deletions": 0, "verification": [], "artifacts": [],
        })
        async with app.state.db.uow() as uow:
            await uow.coding_repositories.create(CodingRepository(
                id=repository_id, name=endpoint, default_branch="master",
                created_by="user_bilal", created_at=now,
            ))
            await uow.coding_repositories.grant_team(TeamRepositoryAccess(
                team_id="team_acme", repository_id=repository_id,
                granted_by="user_bilal", granted_at=now,
            ))
            await uow.coding_runs.create(CodingRun(
                id=run_id, team_id="team_acme", repository_id=repository_id,
                requested_by="user_bilal", agent_id="agent_planner",
                request_id=request_id, instruction="Govern me",
                status="running", created_at=now,
                updated_at=now,
            ))
            stored = await ChangeSetService().record_capture(
                agent_id="agent_planner", request_id=request_id,
                change_set=change_set, uow=uow,
            )
            await ChangeSetService().review(
                change_set_id=stored.id, actor_id="user_bilal", uow=uow
            )
            await uow.commit()
            return stored.id

    change_set_id = asyncio.run(arrange())
    with TestClient(app) as client:
        client.headers["Origin"] = "http://testserver"
        assert client.post(
            "/auth/login", data={"username": "Bilal", "password": "admin123"}
        ).status_code == 200
        decided = client.post(
            f"/management/change-sets/{change_set_id}/{endpoint}",
            follow_redirects=False,
        )
        repeated = client.post(
            f"/management/change-sets/{change_set_id}/{endpoint}",
            follow_redirects=False,
        )

    assert decided.status_code == 303
    assert repeated.status_code == 409

    async def read_back():
        async with app.state.db.uow() as uow:
            return (
                await uow.change_sets.get(change_set_id),
                await uow.change_sets.list_audit(change_set_id),
            )

    stored, audit = asyncio.run(read_back())
    assert stored is not None and stored.status == status
    assert [(event.action, event.actor_id) for event in audit] == [
        ("captured", "agent_planner"),
        ("reviewed", "user_bilal"),
        (action, "user_bilal"),
    ]


class _FakeManager:
    def __init__(self):
        self.sent = []

    def __init__(self):
        self.sent = []
        self.cancel_sent = []

    async def send_coding_run(self, agent_id, *, repository_id, run_id, instruction, timeout, request_id=None):
        self.sent.append((agent_id, repository_id, run_id, instruction, request_id))
        return {"type": "coding_run_ack"}

    async def send_coding_cancel(self, agent_id, *, run_id, request_id=None):
        self.cancel_sent.append((agent_id, run_id))
        return {"type": "coding_run_ack"}


@pytest.mark.asyncio
async def test_dispatch_coding_run_persists_authenticated_queued_run(app):
    now = dt.datetime.now(dt.timezone.utc)
    async with app.state.db.uow() as seed:
        await seed.coding_repositories.create(CodingRepository(
            id="repo_dispatch", name="Dispatch", default_branch="master",
            created_by="user_bilal", created_at=now,
        ))
        await seed.coding_repositories.grant_team(TeamRepositoryAccess(
            team_id="team_acme", repository_id="repo_dispatch",
            granted_by="user_bilal", granted_at=now,
        ))
        await seed.commit()

    fake = _FakeManager()
    async with app.state.db.uow() as uow:
        created = await dispatch_coding_run(
            uow,
            agent_id="agent_planner",
            team_id="team_acme",
            repository_id="repo_dispatch",
            run_id="run_dispatch",
            instruction="Implement the feature",
            requested_by="user_bilal",
            agent_manager=fake,
        )
    assert created.id == "run_dispatch"
    assert created.status == "running"
    assert created.started_at is not None
    assert created.team_id == "team_acme"
    assert created.requested_by == "user_bilal"
    assert created.request_id != "run_dispatch"
    assert fake.sent[0][0] == "agent_planner"
    assert fake.sent[0][1] == "repo_dispatch"
    assert fake.sent[0][2] == "run_dispatch"
    assert fake.sent[0][3] == "Implement the feature"
    assert fake.sent[0][4] == created.request_id

    async with app.state.db.uow() as uow:
        stored = await uow.coding_runs.get("run_dispatch")
    assert stored is not None
    assert stored.status == "running"
    assert stored.started_at is not None
    assert stored.requested_by == "user_bilal"
    assert stored.request_id == created.request_id
    assert stored.request_id != stored.id


@pytest.mark.asyncio
async def test_dispatch_coding_run_rejects_unauthorized_team_repository(app):
    now = dt.datetime.now(dt.timezone.utc)
    async with app.state.db.uow() as seed:
        await seed.coding_repositories.create(CodingRepository(
            id="repo_dispatch_unauth", name="Dispatch unauth", default_branch="master",
            created_by="user_bilal", created_at=now,
        ))
        await seed.commit()

    fake = _FakeManager()
    async with app.state.db.uow() as uow:
        with pytest.raises(PermissionError, match="not authorized"):
            await dispatch_coding_run(
                uow,
                agent_id="agent_planner",
                team_id="team_acme",
                repository_id="repo_dispatch_unauth",
                run_id="run_dispatch_unauth",
                instruction="Must not dispatch",
                requested_by="user_bilal",
                agent_manager=fake,
            )
    assert fake.sent == []
    async with app.state.db.uow() as uow:
        assert await uow.coding_runs.get("run_dispatch_unauth") is None


def test_authenticated_http_start_coding_run_persists_session_identity(app):
    import asyncio
    from starlette.testclient import TestClient

    from crewspace.api.connection import agent_manager

    now = dt.datetime.now(dt.timezone.utc)

    async def arrange():
        async with app.state.db.uow() as uow:
            await uow.coding_repositories.create(CodingRepository(
                id="repo_http", name="HTTP", default_branch="master",
                created_by="user_bilal", created_at=now,
            ))
            await uow.coding_repositories.grant_team(TeamRepositoryAccess(
                team_id="team_acme", repository_id="repo_http",
                granted_by="user_bilal", granted_at=now,
            ))
            await uow.commit()

    asyncio.run(arrange())

    sent = []
    original = agent_manager.send_coding_run

    async def fake_send(agent_id, *, repository_id, run_id, instruction, timeout, request_id=None):
        sent.append((agent_id, repository_id, run_id, instruction, request_id))
        return {"type": "coding_run_ack"}

    agent_manager.send_coding_run = fake_send
    try:
        client = TestClient(app)
        assert client.post("/auth/login", data={"username": "Bilal", "password": "admin123"}).status_code == 200
        response = client.post(
            "/api/coding/runs",
            json={
                "repository_id": "repo_http",
                "agent_id": "agent_planner",
                "instruction": "Ship the feature",
                "team_id": "team_acme",
            },
            headers={"Origin": "http://testserver"},
        )
        assert response.status_code == 200, response.text
        run_id = response.json()["run_id"]
        assert response.json()["status"] == "running"

        async def read_back():
            async with app.state.db.uow() as uow:
                return await uow.coding_runs.get(run_id)

        run = asyncio.run(read_back())
        assert run is not None
        assert run.requested_by == "user_bilal"
        assert run.team_id == "team_acme"
        assert run.request_id != run.id
        assert run.request_id == sent[0][4]
    finally:
        agent_manager.send_coding_run = original




@pytest.mark.asyncio
async def test_coding_run_recent_output_persists_and_restores_after_refresh(app):
    now = dt.datetime.now(dt.timezone.utc)
    async with app.state.db.uow() as seed:
        await seed.coding_repositories.create(CodingRepository(
            id="repo_output", name="Output", default_branch="master",
            created_by="user_bilal", created_at=now,
        ))
        await seed.coding_repositories.grant_team(TeamRepositoryAccess(
            team_id="team_acme", repository_id="repo_output",
            granted_by="user_bilal", granted_at=now,
        ))
        await seed.commit()

    fake = _FakeManager()
    async with app.state.db.uow() as uow:
        await dispatch_coding_run(
            uow, agent_id="agent_planner", team_id="team_acme",
            repository_id="repo_output", run_id="run_output",
            instruction="Build", requested_by="user_bilal", agent_manager=fake,
        )

    # Simulate progress arriving and being persisted, then a client refresh
    # (a brand new unit of work) that must restore status + bounded output.
    async with app.state.db.uow() as uow:
        await uow.coding_runs.append_output("run_output", "line one\n")
        await uow.coding_runs.append_output("run_output", "line two\n")
        await uow.commit()

    async with app.state.db.uow() as uow:
        refreshed = await uow.coding_runs.get("run_output")
    assert refreshed is not None
    assert refreshed.status == "running"
    assert "line one" in refreshed.recent_output
    assert "line two" in refreshed.recent_output


@pytest.mark.asyncio
async def test_coding_run_recent_output_is_bounded(app):
    now = dt.datetime.now(dt.timezone.utc)
    async with app.state.db.uow() as seed:
        await seed.coding_repositories.create(CodingRepository(
            id="repo_bound", name="Bound", default_branch="master",
            created_by="user_bilal", created_at=now,
        ))
        await seed.coding_repositories.grant_team(TeamRepositoryAccess(
            team_id="team_acme", repository_id="repo_bound",
            granted_by="user_bilal", granted_at=now,
        ))
        await seed.commit()

    fake = _FakeManager()
    async with app.state.db.uow() as uow:
        await dispatch_coding_run(
            uow, agent_id="agent_planner", team_id="team_acme",
            repository_id="repo_bound", run_id="run_bound",
            instruction="Build", requested_by="user_bilal", agent_manager=fake,
        )

    chunk = "x" * 2000 + "\n"
    async with app.state.db.uow() as uow:
        for _ in range(100):  # 200_000 bytes >> 64 KiB bound
            await uow.coding_runs.append_output("run_bound", chunk)
        await uow.commit()

    async with app.state.db.uow() as uow:
        stored = await uow.coding_runs.get("run_bound")
    assert len(stored.recent_output.encode("utf-8")) <= 65_536
    # Newest content is retained, oldest is dropped.
    assert stored.recent_output.endswith(chunk)
    assert "line one" not in stored.recent_output


def test_authenticated_http_get_coding_run_returns_status_and_output(app):
    import asyncio
    from starlette.testclient import TestClient

    from crewspace.api.connection import agent_manager

    now = dt.datetime.now(dt.timezone.utc)

    async def arrange():
        async with app.state.db.uow() as uow:
            await uow.coding_repositories.create(CodingRepository(
                id="repo_get", name="Get", default_branch="master",
                created_by="user_bilal", created_at=now,
            ))
            await uow.coding_repositories.grant_team(TeamRepositoryAccess(
                team_id="team_acme", repository_id="repo_get",
                granted_by="user_bilal", granted_at=now,
            ))
            await uow.commit()

    asyncio.run(arrange())

    original = agent_manager.send_coding_run

    async def fake_send(agent_id, *, repository_id, run_id, instruction, timeout, request_id=None):
        return {"type": "coding_run_ack"}

    agent_manager.send_coding_run = fake_send
    try:
        client = TestClient(app)
        assert client.post("/auth/login", data={"username": "Bilal", "password": "admin123"}).status_code == 200
        created = client.post(
            "/api/coding/runs",
            json={
                "repository_id": "repo_get",
                "agent_id": "agent_planner",
                "instruction": "Build",
                "team_id": "team_acme",
            },
            headers={"Origin": "http://testserver"},
        )
        assert created.status_code == 200, created.text
        run_id = created.json()["run_id"]

        async def append():
            async with app.state.db.uow() as uow:
                await uow.coding_runs.append_output(run_id, "progress chunk\n")
                await uow.commit()

        asyncio.run(append())

        response = client.get(f"/api/coding/runs/{run_id}", headers={"Origin": "http://testserver"})
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["status"] == "running"
        assert "progress chunk" in body["recent_output"]

        missing = client.get("/api/coding/runs/does_not_exist", headers={"Origin": "http://testserver"})
        assert missing.status_code == 404
    finally:
        agent_manager.send_coding_run = original


def test_unauthenticated_http_start_coding_run_is_rejected(app):
    from starlette.testclient import TestClient

    client = TestClient(app)
    response = client.post(
        "/api/coding/runs",
        json={
            "repository_id": "repo_http",
            "agent_id": "agent_planner",
            "instruction": "Ship the feature",
            "team_id": "team_acme",
        },
        headers={"Origin": "http://testserver"},
    )
    assert response.status_code in (401, 403, 404)


@pytest.mark.asyncio
async def test_coding_run_requires_team_repository_authorization(app):
    now = dt.datetime.now(dt.timezone.utc)
    async with app.state.db.uow() as uow:
        await uow.coding_repositories.create(CodingRepository(
            id="repo_unauthorized", name="Unauthorized", default_branch="master",
            created_by="user_bilal", created_at=now,
        ))
        with pytest.raises(PermissionError, match="not authorized"):
            await uow.coding_runs.create(CodingRun(
                id="run_unauthorized", team_id="team_acme",
                repository_id="repo_unauthorized", requested_by="user_bilal",
                agent_id="agent_planner", request_id="request_unauthorized",
                instruction="Must not dispatch", status="running", created_at=now,
                updated_at=now,
            ))


@pytest.mark.parametrize(
    ("agent_id", "request_id", "repository_id"),
    [
        ("agent_other", "request_bound", "repo_bound"),
        ("agent_planner", "request_other", "repo_bound"),
        ("agent_planner", "request_bound", "repo_other"),
    ],
)
@pytest.mark.asyncio
async def test_capture_rejects_mismatched_run_ownership(
    app, agent_id: str, request_id: str, repository_id: str
):
    now = dt.datetime.now(dt.timezone.utc)
    async with app.state.db.uow() as uow:
        await uow.coding_repositories.create(CodingRepository(
            id="repo_bound", name="Bound", default_branch="master",
            created_by="user_bilal", created_at=now,
        ))
        await uow.coding_repositories.create(CodingRepository(
            id="repo_other", name="Other", default_branch="master",
            created_by="user_bilal", created_at=now,
        ))
        await uow.coding_repositories.grant_team(TeamRepositoryAccess(
            team_id="team_acme", repository_id="repo_bound",
            granted_by="user_bilal", granted_at=now,
        ))
        await uow.coding_runs.create(CodingRun(
            id="run_bound", team_id="team_acme", repository_id="repo_bound",
            requested_by="user_bilal", agent_id="agent_planner",
            request_id="request_bound", instruction="Bound request",
            status="running", created_at=now,
                updated_at=now,
        ))
        forged = ChangeSetDTO.model_validate({
            "repository_id": repository_id, "run_id": "run_bound",
            "branch": "crewspace/run_bound", "base_commit": "a" * 40,
            "head_commit": "b" * 40, "commits": [], "files": [],
            "additions": 0, "deletions": 0, "verification": [], "artifacts": [],
        })
        with pytest.raises(PermissionError, match="does not match"):
            await ChangeSetService().record_capture(
                agent_id=agent_id, request_id=request_id,
                change_set=forged, uow=uow,
            )
        assert await uow.change_sets.list_for_teams(["team_acme"]) == []
        assert (await uow.coding_runs.get("run_bound")).status == "running"


def test_change_set_index_lists_only_manageable_team_records(app):
    import asyncio

    from starlette.testclient import TestClient

    async def arrange() -> str:
        now = dt.datetime.now(dt.timezone.utc)
        change_set = ChangeSetDTO.model_validate({
            "repository_id": "repo_index", "run_id": "run_index",
            "branch": "crewspace/run_index", "base_commit": "a" * 40,
            "head_commit": "b" * 40, "commits": [], "files": [],
            "additions": 0, "deletions": 0, "verification": [], "artifacts": [],
        })
        async with app.state.db.uow() as uow:
            await uow.coding_repositories.create(CodingRepository(
                id="repo_index", name="Index repository", default_branch="master",
                created_by="user_bilal", created_at=now,
            ))
            await uow.coding_repositories.grant_team(TeamRepositoryAccess(
                team_id="team_acme", repository_id="repo_index",
                granted_by="user_bilal", granted_at=now,
            ))
            await uow.coding_runs.create(CodingRun(
                id="run_index", team_id="team_acme", repository_id="repo_index",
                requested_by="user_bilal", agent_id="agent_planner",
                request_id="request_index", instruction="List me",
                status="running", created_at=now,
                updated_at=now,
            ))
            stored = await ChangeSetService().record_capture(
                agent_id="agent_planner", request_id="request_index",
                change_set=change_set, uow=uow,
            )
            await uow.commit()
            return stored.id

    change_set_id = asyncio.run(arrange())
    with TestClient(app) as client:
        client.headers["Origin"] = "http://testserver"
        assert client.post(
            "/auth/login", data={"username": "Bilal", "password": "admin123"}
        ).status_code == 200
        page = client.get("/management/change-sets")

    assert page.status_code == 200
    assert 'class="sidebar"' in page.text
    assert "Coding change sets" in page.text
    assert f'href="/management/change-sets/{change_set_id}"' in page.text
    assert "repo_index" in page.text
    assert "run_index" in page.text
    assert "Captured" in page.text


@pytest.mark.asyncio
async def test_cancel_coding_run_transitions_to_cancelled_and_is_idempotent(app):
    now = dt.datetime.now(dt.timezone.utc)
    async with app.state.db.uow() as seed:
        await seed.coding_repositories.create(CodingRepository(
            id="repo_cancel", name="Cancel", default_branch="master",
            created_by="user_bilal", created_at=now,
        ))
        await seed.coding_repositories.grant_team(TeamRepositoryAccess(
            team_id="team_acme", repository_id="repo_cancel",
            granted_by="user_bilal", granted_at=now,
        ))
        await seed.commit()

    fake = _FakeManager()
    from crewspace.application.coding_runs import cancel_coding_run

    async with app.state.db.uow() as uow:
        await dispatch_coding_run(
            uow, agent_id="agent_planner", team_id="team_acme",
            repository_id="repo_cancel", run_id="run_cancel",
            instruction="Build", requested_by="user_bilal", agent_manager=fake,
        )

    async with app.state.db.uow() as uow:
        ok = await cancel_coding_run(
            uow, run_id="run_cancel", requested_by="user_bilal", agent_manager=fake,
        )
    assert ok is True
    assert fake.cancel_sent == [("agent_planner", "run_cancel")]

    async with app.state.db.uow() as uow:
        run = await uow.coding_runs.get("run_cancel")
    assert run.status == "cancelled"
    assert run.finished_at is not None

    # Idempotent: a second cancel must not re-transition or re-dispatch a frame.
    async with app.state.db.uow() as uow:
        again = await cancel_coding_run(
            uow, run_id="run_cancel", requested_by="user_bilal", agent_manager=fake,
        )
    assert again is False
    assert fake.cancel_sent == [("agent_planner", "run_cancel")]


@pytest.mark.asyncio
async def test_cancel_coding_run_rejects_unknown_run(app):
    from crewspace.application.coding_runs import cancel_coding_run

    fake = _FakeManager()
    async with app.state.db.uow() as uow:
        with pytest.raises(KeyError):
            await cancel_coding_run(
                uow, run_id="run_missing", requested_by="user_bilal", agent_manager=fake,
            )
    assert fake.cancel_sent == []


def test_authenticated_http_cancel_coding_run(app):
    import asyncio
    from starlette.testclient import TestClient

    from crewspace.api.connection import agent_manager

    now = dt.datetime.now(dt.timezone.utc)

    async def arrange():
        async with app.state.db.uow() as uow:
            await uow.coding_repositories.create(CodingRepository(
                id="repo_cancel_http", name="CancelHTTP", default_branch="master",
                created_by="user_bilal", created_at=now,
            ))
            await uow.coding_repositories.grant_team(TeamRepositoryAccess(
                team_id="team_acme", repository_id="repo_cancel_http",
                granted_by="user_bilal", granted_at=now,
            ))
            await uow.commit()

    asyncio.run(arrange())

    original_cancel = agent_manager.send_coding_cancel
    original_run = agent_manager.send_coding_run

    async def fake_cancel(agent_id, *, run_id, request_id=None):
        return {"type": "coding_run_ack"}

    async def fake_run(agent_id, *, repository_id, run_id, instruction, timeout, request_id=None):
        return {"type": "coding_run_ack"}

    agent_manager.send_coding_cancel = fake_cancel
    agent_manager.send_coding_run = fake_run
    try:
        client = TestClient(app)
        assert client.post("/auth/login", data={"username": "Bilal", "password": "admin123"}).status_code == 200
        created = client.post(
            "/api/coding/runs",
            json={
                "repository_id": "repo_cancel_http",
                "agent_id": "agent_planner",
                "instruction": "Build",
                "team_id": "team_acme",
            },
            headers={"Origin": "http://testserver"},
        )
        assert created.status_code == 200, created.text
        run_id = created.json()["run_id"]

        response = client.post(f"/api/coding/runs/{run_id}/cancel", headers={"Origin": "http://testserver"})
        assert response.status_code == 200, response.text
        assert response.json()["status"] == "cancelled"

        missing = client.post("/api/coding/runs/does_not_exist/cancel", headers={"Origin": "http://testserver"})
        assert missing.status_code == 404
    finally:
        agent_manager.send_coding_cancel = original_cancel
        agent_manager.send_coding_run = original_run


@pytest.mark.asyncio
async def test_reconcile_interrupted_runs_marks_active_as_interrupted(app):
    async def arrange():
        async with app.state.db.uow() as uow:
            await uow.coding_repositories.grant_team(
                TeamRepositoryAccess(
                    team_id="team_acme",
                    repository_id="repo_recon",
                    granted_by="user_bilal",
                    granted_at=dt.datetime.now(dt.timezone.utc),
                )
            )
            await uow.commit()

    async def make_run(run_id, agent_id):
        async with app.state.db.uow() as uow:
            await dispatch_coding_run(
                uow,
                agent_id=agent_id,
                team_id="team_acme",
                repository_id="repo_recon",
                run_id=run_id,
                instruction="work",
                requested_by="user_bilal",
                agent_manager=_FakeManager(),
            )

    await arrange()
    await make_run("run_recon_a", "agent_recon_a")
    await make_run("run_recon_b", "agent_recon_b")
    # A queued run created directly.
    async with app.state.db.uow() as uow:
        from crewspace.domain.entities import CodingRun

        now = dt.datetime.now(dt.timezone.utc)
        queued = CodingRun(
            id="run_recon_q",
            agent_id="agent_recon_a",
            team_id="team_acme",
            repository_id="repo_recon",
            requested_by="user_bilal",
            request_id="req_recon_q",
            instruction="wait",
            status="queued",
            created_at=now,
            updated_at=now,
        )
        await uow.coding_runs.create(queued)
        await uow.commit()
    # A succeeded run that must be left untouched.
    async with app.state.db.uow() as uow:
        await uow.coding_runs.transition(
            "run_recon_a", expected="running", status="succeeded",
            updated_at=dt.datetime.now(dt.timezone.utc), started_at=None, finished_at=None,
        )
        await uow.commit()

    from crewspace.application.coding_runs import reconcile_interrupted_runs

    async with app.state.db.uow() as uow:
        reconciled = await reconcile_interrupted_runs(uow, agent_id=None)
    assert "run_recon_a" not in reconciled  # already succeeded
    assert "run_recon_b" in reconciled
    assert "run_recon_q" in reconciled

    async with app.state.db.uow() as uow:
        assert (await uow.coding_runs.get("run_recon_a")).status == "succeeded"
        assert (await uow.coding_runs.get("run_recon_b")).status == "interrupted"
        assert (await uow.coding_runs.get("run_recon_q")).status == "interrupted"


@pytest.mark.asyncio
async def test_reconcile_is_agent_scoped_and_idempotent(app):
    async def arrange():
        async with app.state.db.uow() as uow:
            await uow.coding_repositories.grant_team(
                TeamRepositoryAccess(
                    team_id="team_acme",
                    repository_id="repo_recon2",
                    granted_by="user_bilal",
                    granted_at=dt.datetime.now(dt.timezone.utc),
                )
            )
            await uow.commit()

    async def make_run(run_id, agent_id):
        async with app.state.db.uow() as uow:
            await dispatch_coding_run(
                uow,
                agent_id=agent_id,
                team_id="team_acme",
                repository_id="repo_recon2",
                run_id=run_id,
                instruction="work",
                requested_by="user_bilal",
                agent_manager=_FakeManager(),
            )

    await arrange()
    await make_run("run_scoped_a", "agent_scoped_a")
    await make_run("run_scoped_b", "agent_scoped_b")

    from crewspace.application.coding_runs import reconcile_interrupted_runs

    async with app.state.db.uow() as uow:
        first = await reconcile_interrupted_runs(
            uow, agent_id="agent_scoped_a"
        )
    assert first == ["run_scoped_a"]
    async with app.state.db.uow() as uow:
        assert (await uow.coding_runs.get("run_scoped_a")).status == "interrupted"
        assert (await uow.coding_runs.get("run_scoped_b")).status == "running"

    # Idempotent: already interrupted -> no further reconciliation.
    async with app.state.db.uow() as uow:
        second = await reconcile_interrupted_runs(
            uow, agent_id="agent_scoped_a"
        )
    assert second == []


@pytest.mark.asyncio
async def test_manager_disconnect_triggers_reconcile_hook():
    from crewspace.api.connection import agent_manager

    original = agent_manager.on_disconnect
    calls = []

    async def hook(agent_id):
        calls.append(agent_id)

    agent_manager.on_disconnect = hook
    try:
        class _StubWs:
            async def accept(self):
                return None

        fake_ws = _StubWs()
        # Simulate the manager holding a connection then losing it.
        await agent_manager.connect("agent_hook_x", fake_ws)
        agent_manager.disconnect("agent_hook_x", fake_ws)
        # Let the fire-and-forget reconcile hook run on the loop.
        await asyncio.sleep(0.01)
        assert calls == ["agent_hook_x"]
    finally:
        agent_manager.on_disconnect = original
