from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from crewspace.infrastructure.git_worktrees import GitWorktreeAllocator


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
        "crewspace.infrastructure.git_worktrees.secrets.token_hex",
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
    assert not worktree_root.exists() or not any(worktree_root.iterdir())


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
