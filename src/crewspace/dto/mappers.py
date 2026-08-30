"""Mappers: domain view models -> DTOs.

Kept in the dto package (the application/api boundary) so the mapping lives at
the seam where the storage shape stops and the API shape begins. Domain and API
layers stay free of each other's types.
"""
from __future__ import annotations

from ..domain.entities import (
    BoardView,
    CardActivityView,
    CardView,
    ColumnView,
    CommentView,
    MessageView,
)
from .board import BoardDTO, CardDetailDTO, CardDTO, ColumnDTO, CommentDTO
from .messages import MessageDTO
from .markdown import render_message_markdown


def to_message(m: MessageView) -> MessageDTO:
    return MessageDTO(
        id=m.id,
        channel_id=m.channel_id,
        body=m.body,
        rendered_body=render_message_markdown(m.body),
        author_id=m.author_id,
        author_name=m.author_name,
        author_kind=m.author_kind.value,
        avatar=m.author_avatar,
        created_at=m.created_at,
        thread_id=m.thread_id,
    )


def to_comment(c: CommentView) -> CommentDTO:
    return CommentDTO(
        id=c.id,
        body=c.body,
        author_id=c.author_id,
        author_name=c.author_name,
        author_kind=c.author_kind.value,
        avatar=c.author_avatar,
        created_at=c.created_at,
    )


def to_card(c: CardView) -> CardDTO:
    return CardDTO(
        id=c.id,
        column_id=c.column_id,
        title=c.title,
        description=c.description,
        assignee_id=c.assignee_id,
        assignee_name=c.assignee_name,
        assignee_avatar=c.assignee_avatar,
        due_date=c.due_date,
        priority=c.priority,
        labels=list(c.labels),
        created_by=c.created_by,
        updated_by=c.updated_by,
        updated_at=c.updated_at,
        created_by_name=c.created_by_name,
        updated_by_name=c.updated_by_name,
        comments=[to_comment(cc) for cc in c.comments],
    )


def to_card_detail(c: CardView, activity: list[CardActivityView]) -> CardDetailDTO:
    return CardDetailDTO(
        card=to_card(c),
        activity=[
            {
                "id": a.id,
                "field": a.field,
                "old_value": a.old_value,
                "new_value": a.new_value,
                "actor_id": a.actor_id,
                "actor_name": a.actor_name,
                "created_at": a.created_at,
            }
            for a in activity
        ],
    )


def to_column(col: ColumnView) -> ColumnDTO:
    return ColumnDTO(
        id=col.id,
        name=col.name,
        position=col.position,
        archived_at=col.archived_at,
        cards=[to_card(c) for c in col.cards],
    )


def to_board(b: BoardView) -> BoardDTO:
    return BoardDTO(
        id=b.id,
        workspace_id=b.workspace_id,
        name=b.name,
        archived_at=b.archived_at,
        columns=[to_column(c) for c in b.columns],
    )
