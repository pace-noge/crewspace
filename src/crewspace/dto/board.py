"""DTOs: the contract between the API layer and the application layer.

Services return these, never domain entities or DB rows. Because the API only
ever sees DTOs, the storage technology is fully encapsulated: swap sqlite for
postgres in infrastructure/ and not a single DTO, route, or template changes.

Pydantic v2 models — they serialize cleanly to JSON (for the REST/WS API) and
are also attribute-accessible from Jinja templates (for HTMX fragments).
"""
from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel


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
    created_by: str | None = None
    updated_by: str | None = None
    updated_at: str | None = None
    created_by_name: str | None = None
    updated_by_name: str | None = None
    comments: list[CommentDTO] = []


class ColumnDTO(BaseModel):
    id: str
    name: str
    position: int
    cards: list[CardDTO] = []


class BoardDTO(BaseModel):
    id: str
    name: str
    columns: list[ColumnDTO] = []
