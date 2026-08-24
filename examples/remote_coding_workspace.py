"""Git-backed isolated workspace allocation for coding runs."""
from __future__ import annotations

import os
import re
import selectors
import secrets
import subprocess
import threading
import time
from pathlib import Path
from typing import Literal, cast

from pydantic import BaseModel, ConfigDict

from crewspace.dto.change_sets import (
    ChangeArtifactDTO,
    ChangeCommitDTO,
    ChangedFileDTO,
    ChangeSetDTO,
    VerificationResultDTO,
)

_SAFE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}\Z")
_GIT_TIMEOUT_SECONDS = 30
_MAX_GIT_OUTPUT_BYTES = 4 * 1024 * 1024
_MAX_CHANGE_ITEMS = 1000
_MAX_ARTIFACTS = 64
_MAX_VERIFICATION_RESULTS = 64


class CodingWorkspaceDTO(BaseModel):
    """Private execution-host workspace state; never sent by the control plane."""

    model_config = ConfigDict(frozen=True)

    repository_id: str
    run_id: str
    path: Path
    branch: str
    base_commit: str


class GitWorktreeAllocator:
    """Allocate isolated worktrees from execution-host repository config."""

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
        self._allocated_workspaces: dict[
            Path, tuple[CodingWorkspaceDTO, tuple[tuple[int, int], tuple[int, int]]]
        ] = {}
        self._capture_locks: dict[Path, threading.Lock] = {}
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
                workspace = CodingWorkspaceDTO(
                    repository_id=repository_id,
                    run_id=run_id,
                    path=path,
                    branch=branch,
                    base_commit=base_commit,
                )
                self._allocated_workspaces[path] = (
                    workspace.model_copy(deep=True),
                    self._workspace_identity(path),
                )
                self._capture_locks[path] = threading.Lock()
                return workspace
            finally:
                lock.rmdir()
        raise RuntimeError("Could not allocate a unique coding workspace")

    def capture(
        self,
        workspace: CodingWorkspaceDTO,
        *,
        verification: list[VerificationResultDTO],
        artifact_paths: list[Path],
    ) -> ChangeSetDTO:
        if len(verification) > _MAX_VERIFICATION_RESULTS:
            raise ValueError("Too many verification results")
        if len(artifact_paths) > _MAX_ARTIFACTS:
            raise ValueError("Too many artifacts")
        path = workspace.path.resolve()
        allocated = self._allocated_workspaces.get(path)
        if allocated is None or allocated[0] != workspace:
            raise ValueError("Coding workspace was not allocated by this allocator")
        with self._capture_locks[path]:
            if allocated[1] != self._workspace_identity(path):
                raise ValueError("Coding workspace identity changed")
            return self._capture_locked(workspace, verification, artifact_paths)

    def _capture_locked(
        self,
        workspace: CodingWorkspaceDTO,
        verification: list[VerificationResultDTO],
        artifact_paths: list[Path],
    ) -> ChangeSetDTO:
        path = workspace.path.resolve()
        before = self._workspace_fingerprint(path)
        if self._git(path, "branch", "--show-current") != workspace.branch:
            raise ValueError("Coding workspace branch changed")
        if self._git(path, "status", "--porcelain", "--untracked-files=no"):
            raise ValueError("Coding workspace has uncommitted tracked changes")
        artifacts = [self._artifact_metadata(path, item) for item in artifact_paths]
        declared_artifacts = {artifact.path for artifact in artifacts}
        untracked = self._untracked_files(path)
        if untracked - declared_artifacts:
            raise ValueError("Coding workspace has undeclared untracked files")
        head_commit = before[0]
        commit_lines = self._git(
            path,
            "log",
            "--reverse",
            "-z",
            "--format=%H%x00%s",
            f"{workspace.base_commit}..{head_commit}",
            preserve=True,
        ).split("\0")
        commit_fields = [field for field in commit_lines if field]
        if len(commit_fields) % 2:
            raise ValueError("Unexpected Git commit output")
        commits = [
            ChangeCommitDTO(sha=commit_fields[i], subject=commit_fields[i + 1])
            for i in range(0, len(commit_fields), 2)
        ]
        if len(commits) > _MAX_CHANGE_ITEMS:
            raise ValueError("Too many commits in change set")

        status_by_path = self._changed_file_statuses(
            path, workspace.base_commit, head_commit
        )
        files: list[ChangedFileDTO] = []
        for line in self._git(
            path,
            "diff",
            "--numstat",
            "--no-renames",
            "-z",
            workspace.base_commit,
            head_commit,
            preserve=True,
        ).split("\0"):
            if not line:
                continue
            additions_text, deletions_text, changed_path = line.split("\t", 2)
            files.append(
                ChangedFileDTO(
                    path=changed_path,
                    status=status_by_path[changed_path],
                    additions=int(additions_text) if additions_text != "-" else 0,
                    deletions=int(deletions_text) if deletions_text != "-" else 0,
                )
            )
        files.sort(key=lambda item: item.path)
        if len(files) > _MAX_CHANGE_ITEMS:
            raise ValueError("Too many files in change set")

        if self._workspace_fingerprint(path) != before:
            raise ValueError("Coding workspace changed during capture")

        return ChangeSetDTO(
            repository_id=workspace.repository_id,
            run_id=workspace.run_id,
            branch=workspace.branch,
            base_commit=workspace.base_commit,
            head_commit=head_commit,
            commits=tuple(commits),
            files=tuple(files),
            additions=sum(item.additions for item in files),
            deletions=sum(item.deletions for item in files),
            verification=tuple(verification),
            artifacts=tuple(artifacts),
        )

    def _changed_file_statuses(
        self, path: Path, base_commit: str, head_commit: str
    ) -> dict[str, Literal["added", "modified", "deleted", "renamed"]]:
        labels = {"A": "added", "M": "modified", "D": "deleted", "R": "renamed"}
        statuses: dict[str, Literal["added", "modified", "deleted", "renamed"]] = {}
        fields = [
            field
            for field in self._git(
                path,
                "diff",
                "--name-status",
                "--no-renames",
                "-z",
                base_commit,
                head_commit,
                preserve=True,
            ).split("\0")
            if field
        ]
        if len(fields) % 2:
            raise ValueError("Unexpected Git file-status output")
        for index in range(0, len(fields), 2):
            code, changed_path = fields[index], fields[index + 1]
            if code not in labels:
                raise ValueError(f"Unsupported Git change status: {code}")
            statuses[changed_path] = cast(
                Literal["added", "modified", "deleted", "renamed"], labels[code]
            )
        return statuses

    def _untracked_files(self, path: Path) -> set[str]:
        ordinary = self._git(
            path, "ls-files", "--others", "--exclude-standard", "-z", preserve=True
        ).split("\0")
        ignored = self._git(
            path,
            "ls-files",
            "--others",
            "--ignored",
            "--exclude-standard",
            "-z",
            preserve=True,
        ).split("\0")
        return {item for item in ordinary + ignored if item}

    def _workspace_fingerprint(self, path: Path) -> tuple[str, str]:
        return (
            self._git(path, "rev-parse", "HEAD"),
            self._git(
                path,
                "status",
                "--porcelain=v1",
                "-z",
                "--ignored=matching",
                preserve=True,
            ),
        )

    @classmethod
    def _workspace_identity(
        cls, path: Path
    ) -> tuple[tuple[int, int], tuple[int, int]]:
        path_stat = path.stat()
        common_dir = Path(cls._git(path, "rev-parse", "--git-common-dir"))
        if not common_dir.is_absolute():
            common_dir = path / common_dir
        common_stat = common_dir.resolve().stat()
        return (
            (path_stat.st_dev, path_stat.st_ino),
            (common_stat.st_dev, common_stat.st_ino),
        )

    @staticmethod
    def _artifact_metadata(workspace_path: Path, artifact_path: Path) -> ChangeArtifactDTO:
        resolved = (workspace_path / artifact_path).resolve()
        if not resolved.is_relative_to(workspace_path) or not resolved.is_file():
            raise ValueError("Artifact must be a file inside the coding workspace")
        return ChangeArtifactDTO(
            path=resolved.relative_to(workspace_path).as_posix(),
            size_bytes=resolved.stat().st_size,
        )

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
        for command in (
            ["git", "-C", str(repository), "worktree", "remove", "--force", str(path)],
            ["git", "-C", str(repository), "branch", "-D", branch],
        ):
            try:
                subprocess.run(
                    command,
                    check=False,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    timeout=_GIT_TIMEOUT_SECONDS,
                )
            except subprocess.TimeoutExpired:
                pass

    @staticmethod
    def _branch_exists(repository: Path, branch: str) -> bool:
        try:
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
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=_GIT_TIMEOUT_SECONDS,
            )
        except subprocess.TimeoutExpired as exc:
            raise ValueError("git command timed out") from exc
        return result.returncode == 0

    @staticmethod
    def _git(repository: Path, *args: str, preserve: bool = False) -> str:
        process = subprocess.Popen(
            ["git", "-C", str(repository), *args],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        selector = selectors.DefaultSelector()
        assert process.stdout is not None
        assert process.stderr is not None
        selector.register(process.stdout, selectors.EVENT_READ)
        selector.register(process.stderr, selectors.EVENT_READ)
        output = {
            process.stdout.fileno(): bytearray(),
            process.stderr.fileno(): bytearray(),
        }
        deadline = time.monotonic() + _GIT_TIMEOUT_SECONDS
        try:
            while selector.get_map():
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    process.kill()
                    raise ValueError("git command timed out")
                for key, _ in selector.select(remaining):
                    stream = key.fileobj
                    chunk = os.read(key.fd, 65536)
                    if not chunk:
                        selector.unregister(stream)
                        continue
                    output[key.fd].extend(chunk)
                    if sum(len(item) for item in output.values()) > _MAX_GIT_OUTPUT_BYTES:
                        process.kill()
                        raise ValueError("git command output exceeded limit")
            return_code = process.wait(timeout=max(0.1, deadline - time.monotonic()))
        except subprocess.TimeoutExpired as exc:
            process.kill()
            raise ValueError("git command timed out") from exc
        finally:
            selector.close()
            if process.poll() is None:
                process.kill()
                process.wait()
        stdout = output[process.stdout.fileno()].decode(errors="replace")
        stderr = output[process.stderr.fileno()].decode(errors="replace")
        if return_code:
            raise ValueError(stderr.strip() or stdout.strip() or "git command failed")
        return stdout if preserve else stdout.strip()
