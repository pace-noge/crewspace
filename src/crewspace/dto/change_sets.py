"""Path-free wire DTOs for remote coding change sets."""
from __future__ import annotations

import re
from pathlib import PurePosixPath
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, field_validator


SafeId = Annotated[str, StringConstraints(pattern=r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}")]
GitOid = Annotated[str, StringConstraints(pattern=r"[0-9a-f]{40,64}")]
BoundedText = Annotated[str, StringConstraints(max_length=4096)]
RelativePath = Annotated[str, StringConstraints(min_length=1, max_length=4096)]


class FrozenDTO(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    @field_validator("*", mode="after")
    @classmethod
    def validate_wire_strings(cls, value, info):
        if not isinstance(value, str):
            return value
        if info.field_name in {"repository_id", "run_id"} and re.fullmatch(
            r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}", value
        ) is None:
            raise ValueError("identifier contains unsafe characters")
        if info.field_name in {"sha", "base_commit", "head_commit"} and re.fullmatch(
            r"(?:[0-9a-f]{40}|[0-9a-f]{64})", value
        ) is None:
            raise ValueError("invalid Git object id")
        if info.field_name == "path":
            path = PurePosixPath(value)
            if (
                path.is_absolute()
                or path.as_posix() == "."
                or value != path.as_posix()
                or ".." in path.parts
                or "\\" in value
                or "\x00" in value
            ):
                raise ValueError("path must be normalized, relative, and traversal-free")
        return value


class ChangeCommitDTO(FrozenDTO):
    sha: GitOid
    subject: BoundedText


class ChangedFileDTO(FrozenDTO):
    path: RelativePath
    status: Literal["added", "modified", "deleted", "renamed"]
    additions: int = Field(ge=0)
    deletions: int = Field(ge=0)


class VerificationResultDTO(FrozenDTO):
    name: Annotated[str, StringConstraints(min_length=1, max_length=128)]
    status: Literal["passed", "failed", "skipped"]
    summary: BoundedText


class ChangeArtifactDTO(FrozenDTO):
    path: RelativePath
    size_bytes: int = Field(ge=0)


class ChangeSetDTO(FrozenDTO):
    repository_id: SafeId
    run_id: SafeId
    branch: Annotated[str, StringConstraints(min_length=1, max_length=255)]
    base_commit: GitOid
    head_commit: GitOid
    commits: tuple[ChangeCommitDTO, ...] = Field(max_length=1000)
    files: tuple[ChangedFileDTO, ...] = Field(max_length=1000)
    additions: int = Field(ge=0)
    deletions: int = Field(ge=0)
    verification: tuple[VerificationResultDTO, ...] = Field(max_length=64)
    artifacts: tuple[ChangeArtifactDTO, ...] = Field(max_length=64)
