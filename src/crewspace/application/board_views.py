"""M7.6 — pure board planning view model.

Turns a ``BoardDTO`` (flattened cards with their metadata) into render-ready
planning projections: filterable card sets, deterministic grouping
(assignee/agent/label/priority/due/status), swimlanes, a due-date timeline, and
light cycle-time/throughput-style aggregates.

Kept free of DB/request dependencies so it is unit-testable without an ``app``
fixture and stays migration-safe (no sqlalchemy import -> ``makemigrations
--check`` cannot drift because of it).
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from typing import List, Literal, Optional, cast

from crewspace.domain.entities import SavedBoardView
from crewspace.domain.ports import UnitOfWork
from crewspace.dto.board import (
    BoardDTO,
    BoardFilterDTO,
    BoardGroupDTO,
    CardDTO,
    SavedBoardViewDTO,
)
from .access import can_access_board

DONE_BUCKET_KEYS = ("done",)


def _status_labels(board: BoardDTO) -> dict[str, str]:
    """Map a column id -> its display name (the canonical status label)."""

    return {c.id: c.name for c in board.columns}


def _is_done(card: CardDTO, status_labels: dict[str, str]) -> bool:
    """A card is 'done' when its column id or the column's name reads done."""
    cid = card.column_id
    if cid.lower() in DONE_BUCKET_KEYS:
        return True
    return (status_labels.get(cid) or "").lower() in DONE_BUCKET_KEYS


@dataclass
class BoardCardMetricsView:
    """Light cycle-time/throughput-style aggregates over a planning view."""

    total_cards: int = 0
    completed_cards: int = 0
    overdue_cards: int = 0


@dataclass
class BoardGroupView:
    """One deterministic bucket of a grouping projection."""

    key: str
    label: str
    cards: List[CardDTO] = field(default_factory=list)


@dataclass
class BoardTimelineBucketView:
    """One due-date bucket in the timeline projection."""

    key: str
    overdue: bool
    cards: List[CardDTO] = field(default_factory=list)


@dataclass
class BoardPlanningView:
    cards: List[CardDTO] = field(default_factory=list)
    groups: List[BoardGroupView] = field(default_factory=list)
    timeline: List[BoardTimelineBucketView] = field(default_factory=list)
    metrics: BoardCardMetricsView = field(default_factory=BoardCardMetricsView)


def _due_bucket(due_date: Optional[str], today: date) -> str:
    """Classify a card's due date into a filter/timeline bucket."""
    if not due_date:
        return "unscheduled"
    try:
        d = date.fromisoformat(due_date)
    except (ValueError, TypeError):
        return "unscheduled"
    if d < today:
        return "overdue"
    if d == today:
        return "today"
    return "upcoming"


def _all_cards(board: BoardDTO) -> List[CardDTO]:
    return [card for column in board.columns for card in column.cards]


def _matches(
    card: CardDTO, f: Optional[BoardFilterDTO], _status_labels: dict[str, str], today: date
) -> bool:
    if f is None:
        return True
    if f.assignee_id and card.assignee_id != f.assignee_id:
        return False
    if f.agent_id:
        if not (card.assignee_id == f.agent_id and card.assignee_kind == "agent"):
            return False
    if f.label and f.label not in (card.labels or []):
        return False
    if f.priority and card.priority != f.priority:
        return False
    if f.due and _due_bucket(card.due_date, today) != f.due:
        return False
    # "status" filter keys against the canonical COLUMN id (URL-safe), never
    # the display name, so a card in column "Todo" is matched by col_todo.
    if f.status and card.column_id != f.status:
        return False
    return True


def _group_status(cards: List[CardDTO], status_labels: dict[str, str]) -> List[BoardGroupView]:
    buckets: dict[str, list[CardDTO]] = {}
    for card in cards:
        buckets.setdefault(card.column_id, []).append(card)
    return [
        BoardGroupView(
            key=k,
            label=status_labels.get(k, k),
            cards=buckets[k],
        )
        for k in buckets
    ]


def _group_assignee(cards: List[CardDTO], *, agents_only: bool) -> List[BoardGroupView]:
    buckets: list[tuple[str, str, list[CardDTO]]] = []
    order: dict[str, int] = {}
    for card in cards:
        if card.assignee_id and (not agents_only or card.assignee_kind == "agent"):
            label = card.assignee_name or card.assignee_id
            key = card.assignee_id
        elif not card.assignee_id:
            key, label = "unassigned", "Unassigned"
        else:
            # agents_only and this card is human-assigned: not part of the view
            continue
        if key not in order:
            order[key] = len(buckets)
            buckets.append((key, label, []))
        buckets[order[key]][2].append(card)
    return [BoardGroupView(key=k, label=l, cards=cs) for k, l, cs in buckets]


def _group_label(cards: List[CardDTO]) -> List[BoardGroupView]:
    buckets: dict[str, list[CardDTO]] = {}
    for card in cards:
        for lab in card.labels or []:
            buckets.setdefault(lab, []).append(card)
    return [
        BoardGroupView(key=lab, label=lab, cards=buckets[lab])
        for lab in sorted(buckets)
    ]


def _group_priority(cards: List[CardDTO]) -> List[BoardGroupView]:
    order = ["urgent", "high", "medium", "low", "unassigned"]
    buckets: dict[str, list[CardDTO]] = {}
    for card in cards:
        key = card.priority or "unassigned"
        buckets.setdefault(key, []).append(card)
    return [
        BoardGroupView(key=k, label=k, cards=buckets[k])
        for k in order
        if k in buckets
    ]


def _group_due(cards: List[CardDTO], today: date) -> List[BoardGroupView]:
    order = ["overdue", "today", "upcoming", "unscheduled"]
    buckets: dict[str, list[CardDTO]] = {}
    for card in cards:
        key = _due_bucket(card.due_date, today)
        buckets.setdefault(key, []).append(card)
    return [
        BoardGroupView(key=k, label=k.capitalize(), cards=buckets[k])
        for k in order
        if k in buckets
    ]


def _timeline(cards: List[CardDTO], today: date) -> List[BoardTimelineBucketView]:
    dated: list[tuple[str, CardDTO]] = []
    unscheduled: List[CardDTO] = []
    for card in cards:
        if card.due_date:
            try:
                d = date.fromisoformat(card.due_date)
            except (ValueError, TypeError):
                unscheduled.append(card)
                continue
            dated.append((card.due_date, card))
        else:
            unscheduled.append(card)
    dated.sort(key=lambda pair: pair[0])
    buckets: list[BoardTimelineBucketView] = []
    for key, group in _group_it(dated):
        cards_in = list(group)
        try:
            overdue = date.fromisoformat(key) < today
        except (ValueError, TypeError):
            overdue = False
        buckets.append(BoardTimelineBucketView(key=key, overdue=overdue, cards=cards_in))
    if unscheduled:
        buckets.append(
            BoardTimelineBucketView(key="unscheduled", overdue=False, cards=unscheduled)
        )
    return buckets


def _group_it(pairs):
    """Yield (key, [items...]) preserving first-seen order."""
    buckets: dict[str, list] = {}
    order: List[str] = []
    for k, item in pairs:
        if k not in buckets:
            buckets[k] = []
            order.append(k)
        buckets[k].append(item)
    return [(k, buckets[k]) for k in order]


def build_board_planning_view(
    board: BoardDTO,
    *,
    filters: Optional[BoardFilterDTO] = None,
    group: Optional[BoardGroupDTO] = None,
    view: str = "board",
    today: Optional[date] = None,
) -> BoardPlanningView:
    """Build a render-ready planning projection of a board's cards.

    - ``filters``: compose assignee/agent/label/priority/due/status filters.
    - ``group``: one deterministic grouping dimension (used for group-"board"
      and "swimlane" views).
    - ``view``: "board" (Kanban, unchanged cards), "swimlane" (group by
      assignee/agent), or "timeline" (group by due date, overdue marked).
    """
    today = today or date.today()
    status_labels = _status_labels(board)
    cards = [
        c for c in _all_cards(board) if _matches(c, filters, status_labels, today)
    ]

    groups: List[BoardGroupView] = []
    timeline: List[BoardTimelineBucketView] = []
    if view == "timeline":
        timeline = _timeline(cards, today)
    elif group is not None:
        if group.by == "status":
            groups = _group_status(cards, status_labels)
        elif group.by == "label":
            groups = _group_label(cards)
        elif group.by == "priority":
            groups = _group_priority(cards)
        elif group.by == "due":
            groups = _group_due(cards, today)
        elif group.by == "agent":
            groups = _group_assignee(cards, agents_only=True)
        else:  # assignee
            groups = _group_assignee(cards, agents_only=False)

    completed_cards = sum(
        1 for c in cards if _is_done(c, status_labels)
    )
    overdue_cards = sum(
        1 for c in cards if _due_bucket(c.due_date, today) == "overdue"
    )
    metrics = BoardCardMetricsView(
        total_cards=len(cards),
        completed_cards=completed_cards,
        overdue_cards=overdue_cards,
    )
    return BoardPlanningView(cards=cards, groups=groups, timeline=timeline, metrics=metrics)


def _to_saved_view_dto(v: SavedBoardView) -> SavedBoardViewDTO:
    return SavedBoardViewDTO(
        id=v.id,
        board_id=v.board_id,
        owner_id=v.owner_id,
        name=v.name,
        view=cast(Literal["board", "swimlane", "timeline"], v.view),
        filters=BoardFilterDTO(**v.filters) if v.filters else None,
        group=BoardGroupDTO(**v.group) if v.group else None,
        created_at=v.created_at,
    )


class BoardSavedViewService:
    """Persist and read private, owner-scoped board planning views.

    Authorization is enforced here, never delegated to the repository:
    - ``save`` requires the principal to be able to access the board;
    - reads are strict OWNER-scoped: a caller may only ever see/delete their own
      views, even when they can access the same board (fail-closed, no reveal);
    - deleting someone else's view raises PermissionError.
    """

    async def save(
        self,
        *,
        board_id: str,
        owner: dict,
        name: str,
        view: str = "board",
        filters: BoardFilterDTO | None = None,
        group: BoardGroupDTO | None = None,
        uow: UnitOfWork,
    ) -> SavedBoardViewDTO:
        if view not in ("board", "swimlane", "timeline"):
            raise ValueError(f"unknown view type: {view}")
        if not await can_access_board(owner, board_id, uow):
            raise PermissionError("Not authorized for this board")
        # Validate the DTO BEFORE inserting: a bad name/view/filter must never
        # leave a row behind when the failure is later raised. Normalize first
        # so whitespace-only names cannot satisfy BoardName(min_length=1).
        clean_name = name.strip()
        if not clean_name:
            raise ValueError("saved view name is required")
        dto = SavedBoardViewDTO(
            id=f"view_{uuid.uuid4().hex[:12]}",
            board_id=board_id,
            owner_id=owner["id"],
            name=clean_name,
            view=cast(Literal["board", "swimlane", "timeline"], view),
            filters=filters,
            group=group,
            created_at=datetime.now(timezone.utc),
        )
        entity = SavedBoardView(
            id=dto.id,
            board_id=dto.board_id,
            owner_id=dto.owner_id,
            name=dto.name,
            view=dto.view,
            filters=filters.model_dump(mode="json") if filters else None,
            group=group.model_dump(mode="json") if group else None,
            created_at=dto.created_at,
        )
        return _to_saved_view_dto(await uow.boards.add_saved_view(entity))

    async def list_for_board(
        self, board_id: str, user: dict, uow: UnitOfWork
    ) -> list[SavedBoardViewDTO]:
        if not await can_access_board(user, board_id, uow):
            return []
        return [
            _to_saved_view_dto(v)
            for v in await uow.boards.list_saved_views(board_id, user["id"])
        ]

    async def get(
        self, view_id: str, user: dict, uow: UnitOfWork
    ) -> SavedBoardViewDTO | None:
        v = await uow.boards.get_saved_view(view_id)
        if v is None:
            return None
        # Strict owner scoping: reveal nothing to a non-owner, even on the same
        # board or when the caller could access the board.
        if v.owner_id != user["id"] or not await can_access_board(
            user, v.board_id, uow
        ):
            return None
        return _to_saved_view_dto(v)

    async def delete(self, view_id: str, user: dict, uow: UnitOfWork) -> None:
        v = await uow.boards.get_saved_view(view_id)
        if v is None:
            return
        if v.owner_id != user["id"] or not await can_access_board(
            user, v.board_id, uow
        ):
            raise PermissionError("Not authorized to delete this saved view")
        await uow.boards.delete_saved_view(view_id)
