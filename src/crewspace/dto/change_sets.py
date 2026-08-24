"""DTOs for isolated coding workspaces and their change sets."""
from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict


class FrozenDTO(BaseModel):
    model_config = ConfigDict(frozen=True)


class CodingWorkspaceDTO(FrozenDTO):
    repository_id: str
    run_id: str
    path: Path
    branch: str
    base_commit: str


class ChangeCommitDTO(FrozenDTO):
    sha: str
    subject: str


class ChangedFileDTO(FrozenDTO):
    path: str
    status: Literal["added", "modified", "deleted", "renamed"]
    additions: int
    deletions: int


class VerificationResultDTO(FrozenDTO):
    name: str
    status: Literal["passed", "failed", "skipped"]
    summary: str


class ChangeArtifactDTO(FrozenDTO):
    path: str
    size_bytes: int


class ChangeSetDTO(FrozenDTO):
    repository_id: str
    run_id: str
    branch: str
    base_commit: str
    head_commit: str
    commits: tuple[ChangeCommitDTO, ...]
    files: tuple[ChangedFileDTO, ...]
    additions: int
    deletions: int
    verification: tuple[VerificationResultDTO, ...]
    artifacts: tuple[ChangeArtifactDTO, ...]
