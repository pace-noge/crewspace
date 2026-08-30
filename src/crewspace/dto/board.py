"""DTOs: the contract between the API layer and the application layer.

Services return these, never domain entities or DB rows. Because the API only
ever sees DTOs, the storage technology is fully encapsulated: swap sqlite for
postgres in infrastructure/ and not a single DTO, route, or template changes.

Pydantic v2 models — they serialize cleanly to JSON (for the REST/WS API) and
are also attribute-accessible from Jinja templates (for HTMX fragments).
"""
from __future__ import annotations

from datetime import datetime
from typing import Annotated

from pydantic import BaseModel, ConfigDict, StringConstraints

SafeId = Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")]
BoardName = Annotated[str, StringConstraints(min_length=1, max_length=120)]


class BoardCommandDTO(BaseModel):
    """Pure, validated command boundary for board create/rename actions."""

    model_config = ConfigDict(frozen=True, extra="forbid")
    workspace_id: SafeId | None = None
    board_id: SafeId | None = None
    name: BoardName


class ColumnCommandDTO(BaseModel):
    """Pure, validated command boundary for column create/rename actions."""

    model_config = ConfigDict(frozen=True, extra="forbid")
    board_id: SafeId
    column_id: SafeId | None = None
    name: BoardName


class CommentDTO(BaseModel):
    id: str
    body: str
    author_id: str
    author_name: str
    author_kind: str
    avatar: str | None = None
    created_at: datetime


class CardDTO(BaseModel):
    id: str
    column_id: str
    title: str
    description: str | None = None
    assignee_id: str | None = None
    assignee_name: str | None = None
    assignee_avatar: str | None = None
    due_date: str | None = None
    priority: str | None = None
    labels: list[str] = []
    created_by: str | None = None
    updated_by: str | None = None
    updated_at: str | None = None
    created_by_name: str | None = None
    updated_by_name: str | None = None
    comments: list[CommentDTO] = []


class CardDetailDTO(BaseModel):
    """The full card detail surface (card + edit history), json-safe for the REST/UI."""

    card: CardDTO
    activity: list[dict] = []


class ColumnDTO(BaseModel):
    id: str
    name: str
    position: int
    archived_at: str | None = None
    cards: list[CardDTO] = []


class BoardDTO(BaseModel):
    id: str
    workspace_id: str
    name: str
    archived_at: str | None = None
    columns: list[ColumnDTO] = []
