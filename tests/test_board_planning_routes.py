"""M7.6 — routed board planning surfaces + saved views (RED first)."""
from __future__ import annotations


def test_board_page_supports_timeline_view(client):
    resp = client.get("/boards/board_main?view=timeline")
    assert resp.status_code == 200
    assert "Timeline" in resp.text
    # The timeline fragment renders due-date buckets.
    assert "unscheduled" in resp.text or "overdue" in resp.text


def test_board_page_supports_swimlane_view(client):
    resp = client.get("/boards/board_main?view=swimlane&group_by=agent")
    assert resp.status_code == 200
    assert "Swimlanes" in resp.text
    assert "Unassigned" in resp.text


def test_board_page_supports_all_filter_and_group_controls(client):
    resp = client.get(
        "/boards/board_main?label=backend&priority=urgent&status=col_todo"
        "&assignee_id=user_bilal&agent_id=agent_planner&group_by=status"
    )
    assert resp.status_code == 200
    assert "board-views-toolbar" in resp.text
    for field in ("assignee_id", "agent_id", "label", "priority", "due", "status", "group_by"):
        assert f'name="{field}"' in resp.text
    assert 'value="backend"' in resp.text
    assert 'value="col_todo"' in resp.text
    assert "Grouped by status" in resp.text


def test_saved_view_create_list_delete_route(app, client):
    # Create a saved view via the authenticated route.
    resp = client.post(
        "/boards/board_main/saved-views",
        data={
            "name": "My urgent",
            "view": "swimlane",
            "group_by": "agent",
            "priority": "urgent",
        },
    )
    assert resp.status_code in (200, 303)

    # It appears in the board page's saved-view menu.
    board = client.get("/boards/board_main")
    assert "My urgent" in board.text
    assert "/boards/board_main/saved-views/" in board.text
