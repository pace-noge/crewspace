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
    """Allocate isolated worktrees from execution-host repository config.

    Ownership, retained markers, and removal tombstones are persisted to an
    optionally-configured durable state file so a process restart reconstructs
    them instead of losing cleanup safeguards (M8.2 — lifts the M6.3 deferral).
    Without a ``durable_state_path`` the allocator behaves exactly as before
    (in-memory only), preserving backward compatibility.
    """

    def __init__(
        self,
        *,
        repositories: dict[str, Path],
        worktree_root: Path,
        durable_state_path: Path | None = None,
    ) -> None:
        self._repositories: dict[str, Path] = {}
        self._repository_identities: dict[
            str, tuple[tuple[int, int], tuple[int, int]]
        ] = {}
        self._allocated_workspaces: dict[
            Path,
            tuple[
                CodingWorkspaceDTO,
                tuple[tuple[int, int], tuple[int, int]],
                tuple[int, int, int],
                str,
                tuple[int, int],
            ],
        ] = {}
        self._capture_locks: dict[Path, threading.Lock] = {}
        self._retained_workspaces: set[Path] = set()
        self._removed_workspaces: set[tuple[str, str, Path, str]] = set()
        self._pending_branch_cleanup: dict[
            Path, tuple[bool, str, tuple[int, int, int]]
        ] = {}
        self._durable_state_path = (
            durable_state_path.expanduser().resolve()
            if durable_state_path is not None
            else None
        )
        self._durable_state_lock = threading.Lock()
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
        if self._durable_state_path is not None:
            self._load_durable_state()

    # ------------------------------------------------------------------
    # Durable state persistence (M8.2)
    # ------------------------------------------------------------------
    def _persist_durable_state(self) -> None:
        """Write allocator ownership/retained/tombstone state atomically.

        Serialized under a lock so concurrent lifecycle transitions cannot race
        on snapshot construction or file rename. Best-effort: persistence
        failures must not corrupt the in-memory contract or crash a lifecycle
        action.
        """
        if self._durable_state_path is None:
            return
        with self._durable_state_lock:
            payload = {
                "version": 1,
                "repositories": {
                    rid: str(repo) for rid, repo in self._repositories.items()
                },
                "allocated": [
                    {
                        "repository_id": allocated[0].repository_id,
                        "run_id": allocated[0].run_id,
                        "path": str(allocated[0].path),
                        "branch": allocated[0].branch,
                        "base_commit": allocated[0].base_commit,
                    }
                    for allocated in self._allocated_workspaces.values()
                ],
                "retained": [str(p) for p in self._retained_workspaces],
                "removed": [
                    {"repository_id": k[0], "run_id": k[1], "path": str(k[2]), "branch": k[3]}
                    for k in self._removed_workspaces
                ],
            }
            try:
                self._durable_state_path.parent.mkdir(parents=True, exist_ok=True)
                import json
                import secrets as _secrets

                tmp = self._durable_state_path.with_suffix(f".tmp.{_secrets.token_hex(4)}")
                tmp.write_text(
                    json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8"
                )
                tmp.replace(self._durable_state_path)
            except OSError as exc:  # best-effort; never break the lifecycle contract
                print(f"[workspace] failed to persist durable state: {exc}", flush=True)

    def _load_durable_state(self) -> None:
        """Reconstruct ownership/retained/removed state after a restart.

        Fail-closed: a retained marker or tombstone persisted on disk is
        authoritative and is never lost. A workspace whose path no longer exists
        is treated as removed (tombstoned) rather than resurrected, so it can
        never be cleaned or deleted again.
        """
        import json

        if not self._durable_state_path.exists():
            return
        try:
            payload = json.loads(self._durable_state_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            print(
                f"[workspace] ignoring unreadable durable state: {exc}", flush=True
            )
            return
        if payload.get("version") != 1:
            return

        # Tombstones first (authoritative, never resurrected).
        for item in payload.get("removed", []):
            self._removed_workspaces.add(
                (
                    item["repository_id"],
                    item["run_id"],
                    Path(item["path"]),
                    item["branch"],
                )
            )

        retained = {Path(p) for p in payload.get("retained", [])}
        self._retained_workspaces.update(retained)

        # Allocated workspaces: reconstruct the DTO + re-derive identity from the
        # live filesystem. Any workspace whose path is gone must be tombstoned
        # (never resurrected as allocatable); if it was retained, mark retained.
        for item in payload.get("allocated", []):
            repo_id = item["repository_id"]
            path = Path(item["path"])
            repo = self._repositories.get(repo_id)
            # Path must belong to _worktree_root (prevent forged state from
            # resurrecting ownership over an unrelated location) and the
            # repository must still be authorized and present.
            if (
                repo is None
                or not path.is_dir()
                or not (path.is_relative_to(self._worktree_root))
            ):
                self._removed_workspaces.add(
                    (repo_id, item["run_id"], path, item["branch"])
                )
                continue
            ws = CodingWorkspaceDTO(
                repository_id=repo_id,
                run_id=item["run_id"],
                path=path,
                branch=item["branch"],
                base_commit=item["base_commit"],
            )
            try:
                identity = (
                    self._workspace_identity(path),
                    self._branch_ref_identity(repo, ws.branch),
                    self._git(repo, "rev-parse", ws.branch),
                    self._branch_reflog_identity(repo, ws.branch),
                )
            except ValueError:
                self._removed_workspaces.add(
                    (repo_id, item["run_id"], path, item["branch"])
                )
                continue
            # The stored tuple layout is (DTO, ws_ident, branch_ref, base_commit,
            # reflog_ident). Reconstructed branch_ref is unused for validation
            # (re-derived each call), so the exact slot value is not load-bearing.
            self._allocated_workspaces[path] = (
                ws,
                identity[0],
                identity[1],
                ws.base_commit,
                identity[3],
            )
            self._capture_locks[path] = threading.Lock()
            if path in retained:
                self._retained_workspaces.add(path)

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
                    self._branch_ref_identity(repository, branch),
                    base_commit,
                    self._branch_reflog_identity(repository, branch),
                )
                self._capture_locks[path] = threading.Lock()
                self._persist_durable_state()
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
            self._validate_workspace_identity(workspace, allocated)
            head_commit = self._git(path, "rev-parse", "HEAD")
            branch_identity = self._validated_branch_ref_identity(
                self._repositories[workspace.repository_id],
                workspace.branch,
                head_commit,
            )
            self._validate_branch_reflog(
                self._repositories[workspace.repository_id],
                workspace.branch,
                expected_identity=allocated[4],
                expected_oid=head_commit,
            )
            if head_commit == allocated[3] and branch_identity != allocated[2]:
                raise ValueError("Coding workspace branch identity changed")
            self._allocated_workspaces[path] = (
                allocated[0],
                allocated[1],
                branch_identity,
                head_commit,
                allocated[4],
            )
            return self._capture_locked(workspace, verification, artifact_paths)

    def apply_workspace_action(
        self,
        *,
        repository_id: str,
        run_id: str,
        branch: str,
        action: str,
    ) -> str:
        if action not in {"cleanup", "discard", "retain"}:
            raise ValueError("Workspace action is invalid")
        workspace = next(
            (
                allocated[0]
                for allocated in self._allocated_workspaces.values()
                if (
                    allocated[0].repository_id,
                    allocated[0].run_id,
                    allocated[0].branch,
                )
                == (repository_id, run_id, branch)
            ),
            None,
        )
        if workspace is None:
            removed = next(
                (
                    key
                    for key in self._removed_workspaces
                    if (key[0], key[1], key[3]) == (repository_id, run_id, branch)
                ),
                None,
            )
            if removed is not None and action in {"cleanup", "discard"}:
                return "already_removed"
            raise ValueError("Coding workspace was not allocated by this allocator")
        if action == "retain":
            return self.retain(workspace)
        return self.cleanup(workspace, discard=action == "discard")

    @staticmethod
    def _workspace_key(workspace: CodingWorkspaceDTO) -> tuple[str, str, Path, str]:
        return (
            workspace.repository_id,
            workspace.run_id,
            workspace.path.resolve(),
            workspace.branch,
        )

    def retain(self, workspace: CodingWorkspaceDTO) -> str:
        path = workspace.path.resolve()
        allocated = self._allocated_workspaces.get(path)
        if allocated is None or allocated[0] != workspace:
            if self._workspace_key(workspace) in self._removed_workspaces:
                raise ValueError("Coding workspace was already removed")
            raise ValueError("Coding workspace was not allocated by this allocator")
        with self._capture_locks[path]:
            self._validate_workspace_identity(workspace, allocated)
            repository = self._repositories[workspace.repository_id]
            head_commit = self._git(path, "rev-parse", "HEAD")
            branch_identity = self._validated_branch_ref_identity(
                repository,
                workspace.branch,
                head_commit,
            )
            self._validate_branch_reflog(
                repository,
                workspace.branch,
                expected_identity=allocated[4],
                expected_oid=head_commit,
            )
            if head_commit == allocated[3] and branch_identity != allocated[2]:
                raise ValueError("Coding workspace branch identity changed")
            self._allocated_workspaces[path] = (
                allocated[0], allocated[1], branch_identity, head_commit, allocated[4]
            )
            if path in self._retained_workspaces:
                return "already_retained"
            self._retained_workspaces.add(path)
            self._persist_durable_state()
            return "retained"

    def cleanup(self, workspace: CodingWorkspaceDTO, *, discard: bool = False) -> str:
        key = self._workspace_key(workspace)
        if key in self._removed_workspaces:
            return "already_removed"
        path = workspace.path.resolve()
        allocated = self._allocated_workspaces.get(path)
        if allocated is None or allocated[0] != workspace:
            raise ValueError("Coding workspace was not allocated by this allocator")
        repository = self._repositories[workspace.repository_id]
        with self._capture_locks[path]:
            pending = self._pending_branch_cleanup.get(path)
            if pending is not None:
                pending_discard, authorized_head, branch_identity = pending
                if pending_discard != discard:
                    raise ValueError("Coding workspace cleanup mode changed")
                self._finish_branch_cleanup(
                    workspace,
                    repository,
                    discard=discard,
                    key=key,
                    authorized_head=authorized_head,
                    branch_identity=branch_identity,
                )
                return "removed"
            self._validate_workspace_identity(workspace, allocated)
            if path in self._retained_workspaces:
                raise ValueError("Coding workspace is retained")
            if self._git(path, "status", "--porcelain=v1", "-z", preserve=True):
                raise ValueError("Coding workspace has uncommitted changes")
            head_commit = self._git(path, "rev-parse", "HEAD")
            branch_identity = self._validated_branch_ref_identity(
                repository, workspace.branch, head_commit
            )
            self._validate_branch_reflog(
                repository,
                workspace.branch,
                expected_identity=allocated[4],
                expected_oid=head_commit,
            )
            if head_commit == allocated[3] and branch_identity != allocated[2]:
                raise ValueError("Coding workspace branch identity changed")
            if not discard and not self._is_ancestor(
                repository, head_commit, self._git(repository, "rev-parse", "HEAD")
            ):
                raise ValueError("Coding workspace branch is not merged")
            self._git(repository, "worktree", "remove", str(path))
            self._pending_branch_cleanup[path] = (
                discard,
                head_commit,
                branch_identity,
            )
            self._finish_branch_cleanup(
                workspace,
                repository,
                discard=discard,
                key=key,
                authorized_head=head_commit,
                branch_identity=branch_identity,
            )
            return "removed"

    def _finish_branch_cleanup(
        self,
        workspace: CodingWorkspaceDTO,
        repository: Path,
        *,
        discard: bool,
        key: tuple[str, str, Path, str],
        authorized_head: str,
        branch_identity: tuple[int, int, int],
    ) -> None:
        path = workspace.path.resolve()
        if self._repository_identity(repository) != self._repository_identities[
            workspace.repository_id
        ]:
            raise ValueError("Authorized repository identity changed")
        if self._git(repository, "rev-parse", workspace.branch) != authorized_head:
            raise ValueError("Coding workspace branch changed")
        if self._branch_ref_identity(repository, workspace.branch) != branch_identity:
            raise ValueError("Coding workspace branch identity changed")
        self._git(repository, "branch", "-D" if discard else "-d", workspace.branch)
        self._pending_branch_cleanup.pop(path, None)
        self._allocated_workspaces.pop(path, None)
        self._retained_workspaces.discard(path)
        self._removed_workspaces.add(key)
        self._capture_locks.pop(path, None)
        self._persist_durable_state()

    @staticmethod
    def _branch_ref_path(repository: Path, branch: str) -> Path:
        return repository / ".git" / "refs" / "heads" / Path(*branch.split("/"))

    @classmethod
    def _branch_ref_identity(
        cls, repository: Path, branch: str
    ) -> tuple[int, int, int]:
        try:
            stat = cls._branch_ref_path(repository, branch).lstat()
        except OSError as exc:
            raise ValueError("Coding workspace branch identity changed") from exc
        return stat.st_dev, stat.st_ino, stat.st_ctime_ns

    @staticmethod
    def _branch_reflog_path(repository: Path, branch: str) -> Path:
        return repository / ".git" / "logs" / "refs" / "heads" / Path(
            *branch.split("/")
        )

    @classmethod
    def _branch_reflog_identity(
        cls, repository: Path, branch: str
    ) -> tuple[int, int]:
        try:
            stat = cls._branch_reflog_path(repository, branch).lstat()
        except OSError as exc:
            raise ValueError("Coding workspace branch provenance changed") from exc
        return stat.st_dev, stat.st_ino

    @classmethod
    def _validate_branch_reflog(
        cls,
        repository: Path,
        branch: str,
        *,
        expected_identity: tuple[int, int],
        expected_oid: str,
    ) -> None:
        if cls._branch_reflog_identity(repository, branch) != expected_identity:
            raise ValueError("Coding workspace branch provenance changed")
        try:
            last_line = cls._branch_reflog_path(repository, branch).read_bytes().splitlines()[-1]
            new_oid = last_line.split(b" ", 2)[1].decode("ascii")
        except (OSError, IndexError, UnicodeDecodeError) as exc:
            raise ValueError("Coding workspace branch provenance changed") from exc
        if re.fullmatch(r"[0-9a-f]{40}", new_oid) is None or new_oid != expected_oid:
            raise ValueError("Coding workspace branch provenance changed")

    def _validate_workspace_identity(
        self,
        workspace: CodingWorkspaceDTO,
        allocated: tuple[
            CodingWorkspaceDTO,
            tuple[tuple[int, int], tuple[int, int]],
            tuple[int, int, int],
            str,
            tuple[int, int],
        ],
    ) -> None:
        repository = self._repositories.get(workspace.repository_id)
        if repository is None or self._repository_identity(repository) != self._repository_identities[
            workspace.repository_id
        ]:
            raise ValueError("Authorized repository identity changed")
        path = workspace.path.resolve()
        path_stat = path.stat()
        if allocated[1][0] != (path_stat.st_dev, path_stat.st_ino):
            raise ValueError("Coding workspace identity changed")
        if allocated[1] != self._workspace_identity(path):
            raise ValueError("Coding workspace identity changed")
        if self._git(path, "branch", "--show-current") != workspace.branch:
            raise ValueError("Coding workspace branch changed")

    def _validated_branch_ref_identity(
        self, repository: Path, branch: str, expected_oid: str
    ) -> tuple[int, int, int]:
        before = self._branch_ref_identity(repository, branch)
        if self._git(repository, "rev-parse", branch) != expected_oid:
            raise ValueError("Coding workspace branch changed")
        after = self._branch_ref_identity(repository, branch)
        if after != before:
            raise ValueError("Coding workspace branch identity changed")
        return after

    @staticmethod
    def _is_ancestor(repository: Path, ancestor: str, descendant: str) -> bool:
        try:
            result = subprocess.run(
                [
                    "git", "-C", str(repository), "merge-base", "--is-ancestor",
                    ancestor, descendant,
                ],
                check=False,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=_GIT_TIMEOUT_SECONDS,
            )
        except subprocess.TimeoutExpired as exc:
            raise ValueError("git command timed out") from exc
        if result.returncode not in {0, 1}:
            raise ValueError("Could not verify whether coding workspace is merged")
        return result.returncode == 0

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
