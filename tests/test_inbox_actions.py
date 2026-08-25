"""M6.8 slice 4 — dedicated app-shell supports filter/assign/ack/resolve."""
from __future__ import annotations

import re

from crewspace.api.rendering import templates
from crewspace.application.inbox import (
    InboxFilters,
    acknowledge_item,
    assign_item,
    build_inbox_item,
    build_inbox_view,
    filter_inbox,
    resolve_item,
)
from crewspace.application.inbox_store import InboxStore, inbox_store


def _items():
    failed = build_inbox_item("coding_run", "r1", "failed", "team_a", summary="Failed run")
    review = build_inbox_item("change_set", "cs1", "captured", "team_a", summary="Review")
    assert failed is not None and review is not None
    return [failed, review]


def test_filter_and_view_counts_are_deterministic():
    items = _items()
    items = acknowledge_item(items, "coding_run:r1")
    visible = filter_inbox(items, InboxFilters(kinds=("review_requested",), only_unacknowledged=True))
    assert [item.item_id for item in visible] == ["change_set:cs1"]
    view = build_inbox_view(items, InboxFilters(only_unacknowledged=True))
    assert view.total == 2 and view.unacknowledged == 1
    assert view.by_kind == {"review_requested": 1}


def test_assign_acknowledge_and_resolve_are_immutable_transitions():
    original = _items()
    assigned = assign_item(original, "coding_run:r1", "u_bilal")
    acknowledged = acknowledge_item(assigned, "coding_run:r1")
    resolved = resolve_item(acknowledged, "coding_run:r1")
    target = next(item for item in resolved if item.item_id == "coding_run:r1")
    assert target.owner_id == "u_bilal"
    assert target.acknowledged is True and target.resolved is True
    assert original[0].owner_id is None and original[0].acknowledged is False


def test_store_scopes_actions_by_team_and_unknown_item_fails_closed():
    store = InboxStore()
    store.reconcile("team_a", [{"source_type":"coding_run","source_id":"r1","status":"failed","team_id":"team_a"}])
    assert store.acknowledge("team_b", "coding_run:r1") is False
    assert store.assign("team_a", "missing", "u") is False
    assert store.assign("team_a", "coding_run:r1", "u_bilal") is True
    assert store.resolve("team_a", "coding_run:r1") is True
    item = store.view("team_a").items[0]
    assert item.owner_id == "u_bilal" and item.resolved is True


def test_template_renders_filter_assign_acknowledge_resolve_controls():
    view = build_inbox_view(_items())
    html = templates.get_template("inbox.html").render(
        view=view,
        team=type("Team", (), {"id":"team_a", "name":"Team A"})(),
        all_kinds=["review_requested", "run_failed"],
    )
    assert "Operational inbox" in html and "Apply filters" in html
    assert "/acknowledge" in html and "/assign" in html and "/resolve" in html
    assert "/api/coding/runs/r1" in html and "/management/change-sets/cs1" in html


def test_app_shell_get_and_action_posts(client):
    response = client.get("/inbox")
    assert response.status_code == 200 and "Operational inbox" in response.text
    assert '<a class="nav-link active" href="/inbox"><span class="ico">📥</span> Inbox</a>' in response.text
    match = re.search(r'name="team_id" value="([^"]+)"', response.text)
    assert match is not None
    team_id = match.group(1)
    inbox_store.reconcile(team_id, [{"source_type":"coding_run","source_id":"r_ui","status":"failed","team_id":team_id,"summary":"UI failure"}])

    ack = client.post(f"/inbox/coding_run:r_ui/acknowledge", data={"team_id":team_id}, follow_redirects=False)
    assert ack.status_code == 303
    assign = client.post(f"/inbox/coding_run:r_ui/assign", data={"team_id":team_id, "owner_id":"u_bilal"}, follow_redirects=False)
    assert assign.status_code == 303
    resolve = client.post(f"/inbox/coding_run:r_ui/resolve", data={"team_id":team_id}, follow_redirects=False)
    assert resolve.status_code == 303
    item = inbox_store.view(team_id).items[0]
    assert item.acknowledged is True and item.owner_id == "u_bilal" and item.resolved is True

    denied = client.post("/inbox/coding_run:r_ui/acknowledge", data={"team_id":"other_team"}, follow_redirects=False)
    assert denied.status_code == 404
