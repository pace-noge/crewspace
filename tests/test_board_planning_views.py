"""M7.6 — pure board planning projections (RED first)."""
from __future__ import annotations

from datetime import date

from crewspace.application.board_views import build_board_planning_view
from crewspace.dto.board import (
    BoardDTO,
    BoardFilterDTO,
    BoardGroupDTO,
    CardDTO,
    ColumnDTO,
)


def _board() -> BoardDTO:
    return BoardDTO(
        id="board_1",
        workspace_id="ws_1",
        name="Delivery",
        columns=[
            ColumnDTO(
                id="col_todo",
                name="Todo",
                position=0,
                cards=[
                    CardDTO(
                        id="c_overdue",
                        column_id="col_todo",
                        title="Fix auth",
                        assignee_id="agent_planner",
                        assignee_name="Planner",
                        assignee_kind="agent",
                        due_date="2026-08-30",
                        priority="urgent",
                        labels=["security", "backend"],
                    ),
                    CardDTO(
                        id="c_today",
                        column_id="col_todo",
                        title="Write docs",
                        assignee_id="user_bilal",
                        assignee_name="Bilal",
                        assignee_kind="human",
                        due_date="2026-08-31",
                        priority="medium",
                        labels=["docs"],
                    ),
                ],
            ),
            ColumnDTO(
                id="col_done",
                name="Done",
                position=1,
                cards=[
                    CardDTO(
                        id="c_done",
                        column_id="col_done",
                        title="Ship release",
                        due_date="2026-09-02",
                        priority="high",
                        labels=["backend"],
                    )
                ],
            ),
        ],
    )


def test_filters_compose_across_assignee_agent_label_priority_due_and_status():
    board = _board()

    by_agent = build_board_planning_view(
        board,
        filters=BoardFilterDTO(agent_id="agent_planner", label="security", priority="urgent"),
        today=date(2026, 8, 31),
    )
    assert [card.id for card in by_agent.cards] == ["c_overdue"]

    by_human = build_board_planning_view(
        board,
        filters=BoardFilterDTO(assignee_id="user_bilal", due="today", status="col_todo"),
        today=date(2026, 8, 31),
    )
    assert [card.id for card in by_human.cards] == ["c_today"]


def test_grouping_by_status_label_and_priority_is_deterministic():
    board = _board()

    status = build_board_planning_view(board, group=BoardGroupDTO(by="status"))
    # Status/grouping keys are the canonical COLUMN IDs (URL-safe), labels come
    # from the column names — a card in column "Todo" is keyed by "col_todo".
    assert [(group.key, [c.id for c in group.cards]) for group in status.groups] == [
        ("col_todo", ["c_overdue", "c_today"]),
        ("col_done", ["c_done"]),
    ]

    labels = build_board_planning_view(board, group=BoardGroupDTO(by="label"))
    assert [(group.key, [c.id for c in group.cards]) for group in labels.groups] == [
        ("backend", ["c_overdue", "c_done"]),
        ("docs", ["c_today"]),
        ("security", ["c_overdue"]),
    ]


def test_swimlanes_distinguish_humans_agents_and_unassigned():
    view = build_board_planning_view(
        _board(), group=BoardGroupDTO(by="agent"), view="swimlane"
    )
    assert [(lane.key, lane.label, [c.id for c in lane.cards]) for lane in view.groups] == [
        ("agent_planner", "Planner", ["c_overdue"]),
        ("unassigned", "Unassigned", ["c_done"]),
    ]


def test_timeline_groups_due_dates_and_marks_overdue():
    view = build_board_planning_view(_board(), view="timeline", today=date(2026, 8, 31))

    assert [(bucket.key, bucket.overdue, [c.id for c in bucket.cards]) for bucket in view.timeline] == [
        ("2026-08-30", True, ["c_overdue"]),
        ("2026-08-31", False, ["c_today"]),
        ("2026-09-02", False, ["c_done"]),
    ]
    assert view.metrics.total_cards == 3
    assert view.metrics.completed_cards == 1
    assert view.metrics.overdue_cards == 1
