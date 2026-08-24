"""Git-backed isolated workspace allocation for coding runs."""
from __future__ import annotations

import re
import secrets
import subprocess
from pathlib import Path

from crewspace.dto.change_sets import CodingWorkspaceDTO

_SAFE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}\Z")


class GitWorktreeAllocator:
    """Allocate isolated worktrees from server-configured repositories."""

    def __init__(
        self,
        *,
        repositories: dict[str, Path],
        worktree_root: Path,
    ) -> None:
        self._repositories = {
            repository_id: path.resolve()
            for repository_id, path in repositories.items()
        }
        self._worktree_root = worktree_root.resolve()

    def allocate(self, *, repository_id: str, run_id: str) -> CodingWorkspaceDTO:
        repository = self._repositories.get(repository_id)
        if repository is None:
            raise ValueError("Repository is not authorized")
        if _SAFE_ID.fullmatch(run_id) is None:
            raise ValueError("Run id contains unsafe characters")
        if not repository.is_dir():
            raise ValueError("Authorized repository does not exist")

        base_commit = self._git(repository, "rev-parse", "HEAD")
        self._worktree_root.mkdir(parents=True, exist_ok=True)
        lock_root = self._worktree_root / ".allocation-locks"
        lock_root.mkdir(exist_ok=True)
        for _ in range(8):
            allocation_id = f"{run_id}-{secrets.token_hex(6)}"
            branch = f"crewspace/{allocation_id}"
            path = self._worktree_root / allocation_id
            if path.exists() or self._branch_exists(repository, branch):
                continue
            lock = lock_root / allocation_id
            try:
                lock.mkdir()
            except FileExistsError:
                continue
            try:
                if path.exists() or self._branch_exists(repository, branch):
                    continue
                try:
                    self._git(
                        repository,
                        "worktree",
                        "add",
                        "-b",
                        branch,
                        str(path),
                        base_commit,
                    )
                except ValueError:
                    self._rollback_partial(repository, path, branch)
                    raise
                return CodingWorkspaceDTO(
                    repository_id=repository_id,
                    run_id=run_id,
                    path=path,
                    branch=branch,
                    base_commit=base_commit,
                )
            finally:
                lock.rmdir()
                try:
                    lock_root.rmdir()
                except OSError:
                    pass
        raise RuntimeError("Could not allocate a unique coding workspace")

    @staticmethod
    def _rollback_partial(repository: Path, path: Path, branch: str) -> None:
        subprocess.run(
            ["git", "-C", str(repository), "worktree", "remove", "--force", str(path)],
            check=False,
            capture_output=True,
            text=True,
        )
        subprocess.run(
            ["git", "-C", str(repository), "branch", "-D", branch],
            check=False,
            capture_output=True,
            text=True,
        )

    @staticmethod
    def _branch_exists(repository: Path, branch: str) -> bool:
        result = subprocess.run(
            [
                "git",
                "-C",
                str(repository),
                "show-ref",
                "--verify",
                "--quiet",
                f"refs/heads/{branch}",
            ],
            check=False,
            capture_output=True,
            text=True,
        )
        return result.returncode == 0

    @staticmethod
    def _git(repository: Path, *args: str) -> str:
        try:
            result = subprocess.run(
                ["git", "-C", str(repository), *args],
                check=True,
                capture_output=True,
                text=True,
            )
        except subprocess.CalledProcessError as exc:
            detail = exc.stderr.strip() or exc.stdout.strip() or "git command failed"
            raise ValueError(detail) from exc
        return result.stdout.strip()
