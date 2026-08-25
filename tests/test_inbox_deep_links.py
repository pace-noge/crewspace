"""M6.8 slice 5 — every supported item has a relevant detail link."""
from __future__ import annotations

from crewspace.api.rendering import templates
from crewspace.application.inbox import build_inbox_item, build_inbox_view


def _item(source_type, source_id, status, *, deep_link_id=None):
    item = build_inbox_item(
        source_type,
        source_id,
        status,
        "team_a",
        summary=f"{source_type} needs attention",
        deep_link_id=deep_link_id,
    )
    assert item is not None
    return item


def test_every_supported_kind_has_relevant_detail_link():
    cases = [
        (_item("coding_run", "run_failed", "failed"), "/api/coding/runs/run_failed"),
        (_item("coding_run", "run_timeout", "timed_out"), "/api/coding/runs/run_timeout"),
        (_item("coding_run", "run_approval", "approval_required"), "/api/coding/runs/run_approval"),
        (_item("change_set", "cs_1", "captured"), "/management/change-sets/cs_1"),
        (_item("workflow_run", "wr_1", "failed", deep_link_id="wf_1"), "/workflows/wf_1"),
        (_item("agent", "agent_planner", "disconnected"), "/direct/agent_planner"),
        (_item("mcp_tool", "jira.create", "pending", deep_link_id="mcp_jira"), "/management/mcp/mcp_jira"),
        (_item("task", "card_stale", "stale", deep_link_id="board_main"), "/board/board_main"),
    ]
    assert {item.kind for item, _ in cases} == {
        "approval_request", "run_failed", "run_timed_out", "agent_disconnected",
        "workflow_failed", "mcp_approval_pending", "review_requested", "stale_task",
    }
    for item, expected in cases:
        assert item.deep_link == expected
        assert item.deep_link.startswith("/") and "{" not in item.deep_link


def test_template_renders_one_inspectable_anchor_per_item():
    items = [
        _item("coding_run", "run_1", "failed"),
        _item("change_set", "cs_1", "captured"),
        _item("workflow_run", "wr_1", "failed", deep_link_id="wf_1"),
        _item("agent", "agent_planner", "disconnected"),
        _item("mcp_tool", "jira.create", "pending", deep_link_id="mcp_jira"),
        _item("task", "card_1", "stale", deep_link_id="board_main"),
    ]
    html = templates.get_template("inbox.html").render(
        view=build_inbox_view(items),
        team=type("Team", (), {"id":"team_a", "name":"Team A"})(),
        all_kinds=sorted({item.kind for item in items}),
    )
    for item in items:
        assert f'href="{item.deep_link}"' in html
    assert html.count('class="inbox-item') == len(items)
