"""Message DTO (chat API<->application contract)."""
from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel


class MessageDTO(BaseModel):
    id: str
    channel_id: str
    body: str
    rendered_body: str
    author_id: str
    author_name: str
    author_kind: str
    avatar: str | None = None
    created_at: datetime
    thread_id: str | None = None
    reply_count: int = 0
