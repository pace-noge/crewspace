"""Git-backed isolated workspace allocation for coding runs."""
from __future__ import annotations

import re
import secrets
import subprocess
from pathlib import Path

from crewspace.config import Settings
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
        self._repositories: dict[str, Path] = {}
        self._repository_identities: dict[
            str, tuple[tuple[int, int], tuple[int, int]]
        ] = {}
        for repository_id, path in repositories.items():
            if _SAFE_ID.fullmatch(repository_id) is None:
                raise ValueError("Repository id contains unsafe characters")
            repository = path.expanduser().resolve()
            if not repository.is_dir():
                raise ValueError("Authorized repository does not exist")
            git_root = Path(
                self._git(repository, "rev-parse", "--show-toplevel")
            ).resolve()
            if git_root != repository:
                raise ValueError("Authorized repository path must be a Git root")
            self._repositories[repository_id] = repository
            self._repository_identities[repository_id] = self._repository_identity(
                repository
            )
        self._worktree_root = worktree_root.expanduser().resolve()
        if any(
            self._worktree_root == repository
            or self._worktree_root.is_relative_to(repository)
            for repository in self._repositories.values()
        ):
            raise ValueError("Coding worktree root must be outside source repositories")

    @classmethod
    def from_settings(cls, settings: Settings) -> "GitWorktreeAllocator":
        return cls(
            repositories={
                repository_id: Path(path)
                for repository_id, path in settings.coding_repositories.items()
            },
            worktree_root=Path(settings.coding_worktree_root),
        )

    def allocate(self, *, repository_id: str, run_id: str) -> CodingWorkspaceDTO:
        repository = self._repositories.get(repository_id)
        if repository is None:
            raise ValueError("Repository is not authorized")
        if _SAFE_ID.fullmatch(run_id) is None:
            raise ValueError("Run id contains unsafe characters")
        if not repository.is_dir():
            raise ValueError("Authorized repository does not exist")
        if self._repository_identity(repository) != self._repository_identities[
            repository_id
        ]:
            raise ValueError("Authorized repository identity changed")

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
        raise RuntimeError("Could not allocate a unique coding workspace")

    @classmethod
    def _repository_identity(
        cls, repository: Path
    ) -> tuple[tuple[int, int], tuple[int, int]]:
        common_dir = Path(cls._git(repository, "rev-parse", "--git-common-dir"))
        if not common_dir.is_absolute():
            common_dir = repository / common_dir
        repository_stat = repository.stat()
        common_dir_stat = common_dir.resolve().stat()
        return (
            (repository_stat.st_dev, repository_stat.st_ino),
            (common_dir_stat.st_dev, common_dir_stat.st_ino),
        )

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
