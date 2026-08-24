from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import subprocess
from pathlib import Path

import pytest

from crewspace.config import Settings
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


def test_from_settings_uses_only_configured_repository_ids(tmp_path: Path):
    repository = _repository(tmp_path)
    settings = Settings(
        coding_repositories={"crewspace": str(repository)},
        coding_worktree_root=str(tmp_path / "worktrees"),
    )

    allocator = GitWorktreeAllocator.from_settings(settings)
    workspace = allocator.allocate(repository_id="crewspace", run_id="run_123")

    assert workspace.path.parent == (tmp_path / "worktrees").resolve()
    with pytest.raises(ValueError, match="not authorized"):
        allocator.allocate(repository_id=str(repository), run_id="run_456")


def test_from_settings_rejects_non_root_repository_paths(tmp_path: Path):
    repository = _repository(tmp_path)
    nested = repository / "src"
    nested.mkdir()
    settings = Settings(
        coding_repositories={"crewspace": str(nested)},
        coding_worktree_root=str(tmp_path / "worktrees"),
    )

    with pytest.raises(ValueError, match="Git root"):
        GitWorktreeAllocator.from_settings(settings)


def test_from_settings_rejects_unsafe_repository_ids_and_nested_worktree_root(
    tmp_path: Path,
):
    repository = _repository(tmp_path)
    unsafe_id = Settings(
        coding_repositories={"../crewspace": str(repository)},
        coding_worktree_root=str(tmp_path / "worktrees"),
    )
    nested_root = Settings(
        coding_repositories={"crewspace": str(repository)},
        coding_worktree_root=str(repository / ".crewspace-worktrees"),
    )

    with pytest.raises(ValueError, match="Repository id"):
        GitWorktreeAllocator.from_settings(unsafe_id)
    with pytest.raises(ValueError, match="outside source repositories"):
        GitWorktreeAllocator.from_settings(nested_root)


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
