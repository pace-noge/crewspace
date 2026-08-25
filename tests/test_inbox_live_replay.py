"""M6.8 slice 6 — live updates + reconnect replay preserve unread counts."""
from __future__ import annotations

import re

from crewspace.application.inbox import build_inbox_item
from crewspace.application.inbox_events import InboxEventStream, count_unread, inbox_events
from crewspace.application.inbox_store import InboxStore, inbox_store


def _item(source_id: str):
    item = build_inbox_item("coding_run", source_id, "failed", "team_a")
    assert item is not None
    return item


def test_publish_is_monotonic_and_duplicate_snapshot_is_silent():
    stream = InboxEventStream()
    first = stream.publish("team_a", [_item("r1")])
    assert [event.sequence for event in first] == [1, 2]
    assert [event.event_type for event in first] == ["upsert", "unread_count"]
    assert first[-1].unread_count == 1
    assert stream.publish("team_a", [_item("r1")]) == []
    assert stream.cursor("team_a") == 2


def test_ack_and_resolve_update_unread_count_and_replay_after_cursor():
    stream = InboxEventStream()
    store = InboxStore()
    # Swap the store's global publisher just for this pure path by publishing snapshots.
    current = store.reconcile("team_a", [{"source_type":"coding_run","source_id":"r1","status":"failed","team_id":"team_a"}])
    stream.publish("team_a", current)
    cursor = stream.cursor("team_a")
    store.acknowledge("team_a", "coding_run:r1")
    current = store.view("team_a").items
    emitted = stream.publish("team_a", current)
    assert emitted[-1].event_type == "unread_count" and emitted[-1].unread_count == 0
    replay = stream.events_after("team_a", cursor)
    assert replay == emitted and [e.sequence for e in replay] == sorted(e.sequence for e in replay)
    assert count_unread(current) == 0


def test_remove_replays_without_gap_and_teams_are_isolated():
    stream = InboxEventStream()
    stream.publish("team_a", [_item("r1"), _item("r2")])
    cursor = stream.cursor("team_a")
    emitted = stream.publish("team_a", [_item("r2")])
    assert emitted[0].event_type == "remove" and emitted[0].item_id == "coding_run:r1"
    assert stream.events_after("team_a", cursor) == emitted
    assert stream.events_after("team_b", 0) == []
    assert stream.events_after("team_a", -99) == stream.events_after("team_a", 0)


def test_replay_endpoint_returns_cursor_and_current_unread_count(client):
    page = client.get("/inbox")
    match = re.search(r'name="team_id" value="([^"]+)"', page.text)
    assert match is not None
    team_id = match.group(1)
    inbox_store.reconcile(team_id, [{"source_type":"coding_run","source_id":"r_live","status":"failed","team_id":team_id}])
    response = client.get(f"/inbox/events?team_id={team_id}&cursor=0")
    assert response.status_code == 200
    body = response.json()
    assert body["cursor"] == inbox_events.cursor(team_id)
    assert body["unread_count"] == 1
    assert any(event["item_id"] == "coding_run:r_live" for event in body["events"])
    cursor = body["cursor"]
    inbox_store.acknowledge(team_id, "coding_run:r_live")
    replay = client.get(f"/inbox/events?team_id={team_id}&cursor={cursor}").json()
    assert replay["unread_count"] == 0
    assert replay["events"][-1]["unread_count"] == 0
    denied = client.get("/inbox/events?team_id=other_team&cursor=0")
    assert denied.status_code == 404
