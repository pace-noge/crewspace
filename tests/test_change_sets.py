from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, cast

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "examples"))

from crewspace.dto.change_sets import VerificationResultDTO
from remote_coding_workspace import CodingWorkspaceDTO, GitWorktreeAllocator
import remote_coding_workspace as git_worktrees


def _git(path: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(path), *args],
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def _repository(tmp_path: Path) -> Path:
    repository = tmp_path / "repository"
    repository.mkdir()
    _git(repository, "init", "-b", "main")
    _git(repository, "config", "user.name", "Crewspace Test")
    _git(repository, "config", "user.email", "crewspace@example.test")
    (repository / "README.md").write_text("seed\n")
    _git(repository, "add", "README.md")
    _git(repository, "commit", "-m", "seed")
    return repository


def test_allocate_creates_unique_branch_and_worktree_for_each_run(tmp_path: Path):
    repository = _repository(tmp_path)
    worktree_root = tmp_path / "worktrees"
    allocator = GitWorktreeAllocator(
        repositories={"crewspace": repository},
        worktree_root=worktree_root,
    )

    first = allocator.allocate(repository_id="crewspace", run_id="run_123")
    second = allocator.allocate(repository_id="crewspace", run_id="run_123")

    assert first.repository_id == "crewspace"
    assert first.run_id == "run_123"
    assert first.base_commit == _git(repository, "rev-parse", "HEAD")
    assert first.branch.startswith("crewspace/run_123-")
    assert first.path.parent == worktree_root.resolve()
    assert first.path != second.path
    assert first.branch != second.branch
    assert first.path.is_dir()
    assert second.path.is_dir()
    assert _git(first.path, "branch", "--show-current") == first.branch
    assert _git(second.path, "branch", "--show-current") == second.branch


def test_allocate_retries_an_allocation_id_collision(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    repository = _repository(tmp_path)
    suffixes = iter(["collision", "collision", "replacement"])
    monkeypatch.setattr(
        git_worktrees.secrets,
        "token_hex",
        lambda _: next(suffixes),
    )
    allocator = GitWorktreeAllocator(
        repositories={"crewspace": repository},
        worktree_root=tmp_path / "worktrees",
    )

    first = allocator.allocate(repository_id="crewspace", run_id="run_123")
    second = allocator.allocate(repository_id="crewspace", run_id="run_123")

    assert first.path.name == "run_123-collision"
    assert second.path.name == "run_123-replacement"
    assert second.path.is_dir()


def test_allocate_rolls_back_partial_worktree_when_git_hook_fails(tmp_path: Path):
    repository = _repository(tmp_path)
    hook = repository / ".git" / "hooks" / "post-checkout"
    hook.write_text("#!/bin/sh\nexit 1\n")
    hook.chmod(0o755)
    allocator = GitWorktreeAllocator(
        repositories={"crewspace": repository},
        worktree_root=tmp_path / "worktrees",
    )

    with pytest.raises(ValueError):
        allocator.allocate(repository_id="crewspace", run_id="run_123")

    assert "crewspace/run_123-" not in _git(repository, "branch", "--list")
    assert "worktrees/run_123-" not in _git(repository, "worktree", "list")
    worktree_root = tmp_path / "worktrees"
    entries = list(worktree_root.iterdir()) if worktree_root.exists() else []
    assert entries in ([], [worktree_root / ".allocation-locks"])
    lock_root = worktree_root / ".allocation-locks"
    assert not lock_root.exists() or not any(lock_root.iterdir())


@pytest.mark.parametrize(
    "repository_id,run_id",
    [
        ("missing", "run_123"),
        ("crewspace", "../escape"),
        ("crewspace", "run/escape"),
        ("crewspace", ""),
    ],
)
def test_allocate_rejects_unknown_repository_and_unsafe_run_ids(
    tmp_path: Path, repository_id: str, run_id: str
):
    repository = _repository(tmp_path)
    allocator = GitWorktreeAllocator(
        repositories={"crewspace": repository},
        worktree_root=tmp_path / "worktrees",
    )

    with pytest.raises(ValueError):
        allocator.allocate(repository_id=repository_id, run_id=run_id)

    worktree_root = tmp_path / "worktrees"
    assert not worktree_root.exists() or not any(worktree_root.iterdir())


def test_remote_host_config_uses_only_configured_repository_ids(tmp_path: Path):
    repository = _repository(tmp_path)
    allocator = GitWorktreeAllocator(
        repositories={"crewspace": repository},
        worktree_root=tmp_path / "worktrees",
    )

    workspace = allocator.allocate(repository_id="crewspace", run_id="run_123")

    assert workspace.path.parent == (tmp_path / "worktrees").resolve()
    with pytest.raises(ValueError, match="not authorized"):
        allocator.allocate(repository_id=str(repository), run_id="run_456")


def test_remote_host_config_rejects_non_root_repository_paths(tmp_path: Path):
    repository = _repository(tmp_path)
    nested = repository / "src"
    nested.mkdir()

    with pytest.raises(ValueError, match="Git root"):
        GitWorktreeAllocator(
            repositories={"crewspace": nested},
            worktree_root=tmp_path / "worktrees",
        )


def test_remote_host_config_rejects_unsafe_ids_and_nested_worktree_root(
    tmp_path: Path,
):
    repository = _repository(tmp_path)

    with pytest.raises(ValueError, match="Repository id"):
        GitWorktreeAllocator(
            repositories={"../crewspace": repository},
            worktree_root=tmp_path / "worktrees",
        )
    with pytest.raises(ValueError, match="outside source repositories"):
        GitWorktreeAllocator(
            repositories={"crewspace": repository},
            worktree_root=repository / ".crewspace-worktrees",
        )


def test_allocate_rejects_repository_replaced_after_authorization(tmp_path: Path):
    repository = _repository(tmp_path)
    allocator = GitWorktreeAllocator(
        repositories={"crewspace": repository},
        worktree_root=tmp_path / "worktrees",
    )
    original = tmp_path / "original-repository"
    repository.rename(original)
    replacement = _repository(tmp_path)
    assert replacement == repository

    with pytest.raises(ValueError, match="identity changed"):
        allocator.allocate(repository_id="crewspace", run_id="run_123")

    assert not (tmp_path / "worktrees").exists()


def test_concurrent_allocations_never_share_branch_or_checkout(tmp_path: Path):
    repository = _repository(tmp_path)
    allocator = GitWorktreeAllocator(
        repositories={"crewspace": repository},
        worktree_root=tmp_path / "worktrees",
    )

    with ThreadPoolExecutor(max_workers=4) as pool:
        workspaces = list(
            pool.map(
                lambda index: allocator.allocate(
                    repository_id="crewspace", run_id=f"run_{index}"
                ),
                range(8),
            )
        )

    assert len({workspace.path for workspace in workspaces}) == 8
    assert len({workspace.branch for workspace in workspaces}) == 8
    assert all(workspace.path.is_dir() for workspace in workspaces)
    listed = _git(repository, "worktree", "list", "--porcelain")
    assert all(str(workspace.path) in listed for workspace in workspaces)


def test_capture_returns_commits_files_verification_and_artifact_metadata(
    tmp_path: Path,
):
    repository = _repository(tmp_path)
    allocator = GitWorktreeAllocator(
        repositories={"crewspace": repository},
        worktree_root=tmp_path / "worktrees",
    )
    workspace = allocator.allocate(repository_id="crewspace", run_id="run_123")
    (workspace.path / "README.md").write_text("seed\nupdated\n")
    (workspace.path / "feature.py").write_text("print('ready')\n")
    _git(workspace.path, "add", "README.md", "feature.py")
    _git(workspace.path, "commit", "-m", "add feature")
    artifact = workspace.path / "reports" / "pytest.xml"
    artifact.parent.mkdir()
    artifact.write_text("<testsuite tests='1'/>\n")

    change_set = allocator.capture(
        workspace,
        verification=[
            VerificationResultDTO(
                name="pytest",
                status="passed",
                summary="1 passed",
            )
        ],
        artifact_paths=[Path("reports/pytest.xml")],
    )

    assert change_set.repository_id == "crewspace"
    assert change_set.run_id == "run_123"
    assert change_set.branch == workspace.branch
    assert change_set.base_commit == workspace.base_commit
    assert change_set.head_commit == _git(workspace.path, "rev-parse", "HEAD")
    assert [(commit.subject, commit.sha) for commit in change_set.commits] == [
        ("add feature", change_set.head_commit)
    ]
    assert [
        (file.path, file.status, file.additions, file.deletions)
        for file in change_set.files
    ] == [
        ("README.md", "modified", 1, 0),
        ("feature.py", "added", 1, 0),
    ]
    assert change_set.additions == 2
    assert change_set.deletions == 0
    assert change_set.verification[0].status == "passed"
    assert change_set.artifacts[0].path == "reports/pytest.xml"
    assert change_set.artifacts[0].size_bytes == artifact.stat().st_size


def test_capture_rejects_forged_workspace_and_dirty_tracked_changes(tmp_path: Path):
    repository = _repository(tmp_path)
    allocator = GitWorktreeAllocator(
        repositories={"crewspace": repository},
        worktree_root=tmp_path / "worktrees",
    )
    workspace = allocator.allocate(repository_id="crewspace", run_id="run_123")
    forged = CodingWorkspaceDTO(
        repository_id=workspace.repository_id,
        run_id=workspace.run_id,
        path=repository,
        branch="main",
        base_commit=workspace.base_commit,
    )

    with pytest.raises(ValueError, match="not allocated"):
        allocator.capture(forged, verification=[], artifact_paths=[])

    (workspace.path / "README.md").write_text("dirty\n")
    with pytest.raises(ValueError, match="uncommitted tracked changes"):
        allocator.capture(workspace, verification=[], artifact_paths=[])


def test_capture_rejects_artifact_traversal_and_external_symlinks(tmp_path: Path):
    repository = _repository(tmp_path)
    allocator = GitWorktreeAllocator(
        repositories={"crewspace": repository},
        worktree_root=tmp_path / "worktrees",
    )
    workspace = allocator.allocate(repository_id="crewspace", run_id="run_123")
    outside = tmp_path / "outside.txt"
    outside.write_text("secret\n")
    (workspace.path / "linked-report").symlink_to(outside)

    for artifact_path in (Path("../../outside.txt"), Path("linked-report")):
        with pytest.raises(ValueError, match="inside the coding workspace"):
            allocator.capture(
                workspace,
                verification=[],
                artifact_paths=[artifact_path],
            )


def test_capture_rejects_undeclared_untracked_files(tmp_path: Path):
    repository = _repository(tmp_path)
    allocator = GitWorktreeAllocator(
        repositories={"crewspace": repository},
        worktree_root=tmp_path / "worktrees",
    )
    workspace = allocator.allocate(repository_id="crewspace", run_id="run_123")
    (workspace.path / "forgotten.py").write_text("print('untracked')\n")

    with pytest.raises(ValueError, match="undeclared untracked files"):
        allocator.capture(workspace, verification=[], artifact_paths=[])


def test_workspace_dto_is_immutable_and_ignored_files_are_not_omitted(tmp_path: Path):
    repository = _repository(tmp_path)
    allocator = GitWorktreeAllocator(
        repositories={"crewspace": repository},
        worktree_root=tmp_path / "worktrees",
    )
    workspace = allocator.allocate(repository_id="crewspace", run_id="run_123")

    with pytest.raises(Exception):
        workspace.branch = "main"

    (workspace.path / ".gitignore").write_text("ignored.log\n")
    _git(workspace.path, "add", ".gitignore")
    _git(workspace.path, "commit", "-m", "ignore logs")
    (workspace.path / "ignored.log").write_text("must not disappear\n")

    with pytest.raises(ValueError, match="undeclared untracked files"):
        allocator.capture(workspace, verification=[], artifact_paths=[])


def test_capture_handles_rename_and_tab_in_filename_consistently(tmp_path: Path):
    repository = _repository(tmp_path)
    allocator = GitWorktreeAllocator(
        repositories={"crewspace": repository},
        worktree_root=tmp_path / "worktrees",
    )
    workspace = allocator.allocate(repository_id="crewspace", run_id="run_123")
    odd_name = "odd\tname.py"
    (workspace.path / odd_name).write_text("print('one')\n")
    _git(workspace.path, "add", odd_name)
    _git(workspace.path, "commit", "-m", "add odd file")
    renamed = "renamed\tfile.py"
    _git(workspace.path, "mv", odd_name, renamed)
    _git(workspace.path, "commit", "-m", "rename odd file")

    change_set = allocator.capture(workspace, verification=[], artifact_paths=[])

    assert [(item.path, item.status) for item in change_set.files] == [
        (renamed, "added")
    ]


def test_capture_preserves_whitespace_paths_and_returns_deeply_frozen_collections(
    tmp_path: Path,
):
    repository = _repository(tmp_path)
    allocator = GitWorktreeAllocator(
        repositories={"crewspace": repository},
        worktree_root=tmp_path / "worktrees",
    )
    workspace = allocator.allocate(repository_id="crewspace", run_id="run_123")
    artifact = workspace.path / "report.log"
    artifact.write_text("declared\n")
    (workspace.path / " report.log").write_text("undeclared\n")

    with pytest.raises(ValueError, match="undeclared untracked files"):
        allocator.capture(workspace, verification=[], artifact_paths=[artifact])

    (workspace.path / " report.log").unlink()
    change_set = allocator.capture(
        workspace, verification=[], artifact_paths=[artifact]
    )
    assert isinstance(change_set.commits, tuple)
    assert isinstance(change_set.files, tuple)
    assert isinstance(change_set.verification, tuple)
    assert isinstance(change_set.artifacts, tuple)


def test_capture_revalidates_workspace_identity_after_acquiring_lock(tmp_path: Path):
    repository = _repository(tmp_path)
    allocator = GitWorktreeAllocator(
        repositories={"crewspace": repository},
        worktree_root=tmp_path / "worktrees",
    )
    workspace = allocator.allocate(repository_id="crewspace", run_id="run_123")
    original = workspace.path.with_name(f"{workspace.path.name}-original")

    class ReplacingLock:
        def __enter__(self):
            workspace.path.rename(original)
            workspace.path.mkdir()
            _git(workspace.path, "init", "-b", "main")
            _git(workspace.path, "config", "user.name", "Crewspace Test")
            _git(workspace.path, "config", "user.email", "crewspace@example.test")
            (workspace.path / "README.md").write_text("replacement\n")
            _git(workspace.path, "add", "README.md")
            _git(workspace.path, "commit", "-m", "replacement")

        def __exit__(self, exc_type, exc, traceback):
            return False

    allocator._capture_locks[workspace.path] = cast(Any, ReplacingLock())

    with pytest.raises(ValueError, match="identity changed"):
        allocator.capture(workspace, verification=[], artifact_paths=[])


def test_git_timeout_is_enforced_after_partial_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    fake_git = tmp_path / "git"
    fake_git.write_text(
        "#!/usr/bin/env python3\n"
        "import sys, time\n"
        "sys.stdout.write('x')\n"
        "sys.stdout.flush()\n"
        "time.sleep(1)\n"
    )
    fake_git.chmod(0o755)
    monkeypatch.setenv("PATH", f"{tmp_path}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setattr(git_worktrees, "_GIT_TIMEOUT_SECONDS", 0.1)

    started = time.monotonic()
    with pytest.raises(ValueError, match="timed out"):
        GitWorktreeAllocator._git(tmp_path, "status")

    assert time.monotonic() - started < 0.5
