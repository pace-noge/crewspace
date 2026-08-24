"""Team-scoped persistence and governance for remote coding change sets."""
from __future__ import annotations

import datetime as dt

import pytest

from crewspace.api.routers.agents import (
    _handle_coding_change_set,
    _persist_coding_change_set,
)
from crewspace.application.change_sets import ChangeSetService
from crewspace.domain.entities import CodingRepository, CodingRun, TeamRepositoryAccess
from crewspace.dto.change_sets import ChangeSetDTO


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
        assert (await uow.coding_runs.get("run_ingress")).status == "captured"
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
