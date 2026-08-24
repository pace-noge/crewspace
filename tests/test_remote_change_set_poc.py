from __future__ import annotations

import datetime as dt
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "examples"))

from claude_code_agent import _workspace_action_response
from remote_coding_workspace import GitWorktreeAllocator

from crewspace.application.change_sets import ChangeSetService, execute_workspace_decision
from crewspace.domain.entities import CodingRepository, CodingRun, TeamRepositoryAccess
from crewspace.dto.change_sets import VerificationResultDTO


def _git(path: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(path), *args],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _repository(tmp_path: Path) -> Path:
    repository = tmp_path / "repository"
    repository.mkdir()
    _git(repository, "init", "-b", "main")
    _git(repository, "config", "user.name", "Crewspace POC")
    _git(repository, "config", "user.email", "crewspace-poc@example.test")
    (repository / "README.md").write_text("seed\n")
    _git(repository, "add", "README.md")
    _git(repository, "commit", "-m", "seed")
    return repository


@pytest.mark.asyncio
async def test_real_repository_change_set_is_captured_reviewed_and_cleaned(
    app, tmp_path: Path
):
    repository = _repository(tmp_path)
    allocator = GitWorktreeAllocator(
        repositories={"poc_repo": repository},
        worktree_root=tmp_path / "worktrees",
    )
    workspace = allocator.allocate(repository_id="poc_repo", run_id="run_poc")
    (workspace.path / "feature.py").write_text("def ready():\n    return True\n")
    _git(workspace.path, "add", "feature.py")
    _git(workspace.path, "commit", "-m", "add verified feature")
    change_set = allocator.capture(
        workspace,
        verification=[
            VerificationResultDTO(
                name="pytest", status="passed", summary="POC verification passed"
            )
        ],
        artifact_paths=[],
    )

    now = dt.datetime.now(dt.timezone.utc)
    async with app.state.db.uow() as uow:
        await uow.coding_repositories.create(
            CodingRepository(
                id="poc_repo",
                name="POC repository",
                default_branch="main",
                created_by="user_bilal",
                created_at=now,
            )
        )
        await uow.coding_repositories.grant_team(
            TeamRepositoryAccess(
                team_id="team_acme",
                repository_id="poc_repo",
                granted_by="user_bilal",
                granted_at=now,
            )
        )
        await uow.coding_runs.create(
            CodingRun(
                id="run_poc",
                team_id="team_acme",
                repository_id="poc_repo",
                requested_by="user_bilal",
                agent_id="agent_planner",
                request_id="request_poc",
                instruction="Produce a real verified change set",
                status="running",
                created_at=now,
                updated_at=now,
            )
        )
        stored = await ChangeSetService().record_capture(
            agent_id="agent_planner",
            request_id="request_poc",
            change_set=change_set,
            uow=uow,
        )
        reviewed = await ChangeSetService().review(
            change_set_id=stored.id,
            actor_id="user_bilal",
            uow=uow,
        )
        await uow.commit()
    assert reviewed.status == "reviewed"
    assert change_set.files[0].path == "feature.py"
    assert change_set.verification[0].status == "passed"

    action_frame = {
        "type": "coding_workspace_action",
        "request_id": "workspace_action_poc",
        "repository_id": "poc_repo",
        "run_id": "run_poc",
        "branch": workspace.branch,
        "action": "discard",
    }

    class RemoteManager:
        async def send_workspace_action(self, agent_id: str, **kwargs):
            assert agent_id == "agent_planner"
            assert "path" not in kwargs
            response = _workspace_action_response(
                allocator,
                {"request_id": action_frame["request_id"], **kwargs},
            )
            assert "path" not in repr(response)
            return response["result"]

    completed = await execute_workspace_decision(
        db=app.state.db,
        manager=RemoteManager(),
        change_set_id=stored.id,
        decision="request_discard",
        current_user={"id": "user_bilal", "role": "superadmin"},
        timeout=30.0,
    )

    assert completed.status == "discarded"
    assert not workspace.path.exists()
    assert _git(repository, "branch", "--list", workspace.branch) == ""
    repeated = _workspace_action_response(allocator, action_frame)
    assert repeated["result"]["status"] == "already_removed"

    async with app.state.db.uow() as uow:
        audit = await uow.change_sets.list_audit(stored.id)
    assert [event.action for event in audit] == [
        "captured",
        "reviewed",
        "discard_requested",
        "workspace_discarded",
    ]
