"""DTOs for isolated coding workspaces and their change sets."""
from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel


class CodingWorkspaceDTO(BaseModel):
    repository_id: str
    run_id: str
    path: Path
    branch: str
    base_commit: str
