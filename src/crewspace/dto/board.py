"""DTOs: the contract between the API layer and the application layer.

Services return these, never domain entities or DB rows. Because the API only
ever sees DTOs, the storage technology is fully encapsulated: swap sqlite for
postgres in infrastructure/ and not a single DTO, route, or template changes.

Pydantic v2 models — they serialize cleanly to JSON (for the REST/WS API) and
are also attribute-accessible from Jinja templates (for HTMX fragments).
"""
from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, StringConstraints

SafeId = Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")]
BoardName = Annotated[str, StringConstraints(min_length=1, max_length=120)]


class BoardDeltaDTO(BaseModel):
    """Pure wire contract for one minimal board mutation.

    ``card_html`` / ``comment_html`` are canonical server-rendered fragments so
    non-acting clients can update one card/comment in place without reloading
    or re-rendering the whole board.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")
    kind: Literal["card_created", "card_moved", "card_updated", "comment_added"]
    card_id: SafeId
    comment_id: SafeId | None = None
    title: str | None = None
    from_column_id: SafeId | None = None
    to_column_id: SafeId | None = None
    card_html: str | None = None
    comment_html: str | None = None


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


class CardRunStatusDTO(BaseModel):
    """Live, authorization-scoped projection of one run linked to a card."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    card_id: SafeId
    run_id: SafeId
    run_status: Literal[
        "queued", "running", "succeeded", "failed", "cancelled", "timed_out", "interrupted"
    ]
    change_set_id: SafeId | None = None
    change_set_status: Literal[
        "captured",
        "reviewed",
        "pr_requested",
        "retain_requested",
        "retained",
        "discard_requested",
        "discarded",
    ] | None = None
    linked_by: SafeId
    linked_at: datetime


# Canonical deep-link targets for linked runs / change sets (view model).
RUN_DETAIL_HREF = "/api/coding/runs/{run_id}"
CHANGE_SET_HREF = "/management/change-sets/{change_set_id}"
CHANGE_SET_REVIEW_HREF = "/management/change-sets/{change_set_id}/review"


def card_run_badges(status: CardRunStatusDTO) -> list[dict[str, str]]:
    """Render-ready badges for one linked run (label + deep link href).

    Built here (not in the template) so route targets stay in one canonical
    place and the template never invents URLs.
    """
    badges: list[dict[str, str]] = [
        {"label": f"run {status.run_status}", "href": RUN_DETAIL_HREF.format(run_id=status.run_id)}
    ]
    if status.change_set_id:
        badges.append({
            "label": f"change set {status.change_set_status or 'captured'}",
            "href": CHANGE_SET_HREF.format(change_set_id=status.change_set_id),
        })
        if status.change_set_status == "captured":
            badges.append({
                "label": "review",
                "href": CHANGE_SET_REVIEW_HREF.format(change_set_id=status.change_set_id),
            })
    return badges


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


class ColumnTriggerDTO(BaseModel):
    """Pure wire contract for a per-column → workflow rule (config surface)."""

    column_id: str
    workflow_id: str | None = None
    enabled: bool = False


class BoardDTO(BaseModel):
    id: str
    workspace_id: str
    name: str
    archived_at: str | None = None
    columns: list[ColumnDTO] = []
