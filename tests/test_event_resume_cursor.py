"""M6.4 slice 3 — Reconnect resume cursor (acceptance item 3).

Acceptance item 3: "Reconnect resumes from a cursor without gaps or duplicate
UI entries." The cursor is positional over the canonical total order produced by
`order_key`: encoding the last-delivered boundary as an opaque, serializable
token. ``events_after`` returns the strictly-later tail, deduped by event id, so
a reconnect replays exactly the gap-free, duplicate-free remainder. This is the
pure-DTO contract; persistence and the multi-worker dedupe store plug in later.
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from crewspace.dto.events import (
    EventDedupeStore,
    EventEnvelope,
    ResumeCursor,
    RunSequencer,
    build_event,
    events_after,
    new_event_id,
    order_key,
)


def _now() -> datetime:
    return datetime(2026, 8, 25, 11, 0, 0, tzinfo=timezone.utc)


def _emit(seq: RunSequencer, rid: str, n: int) -> list[EventEnvelope]:
    return [
        build_event(
            "command",
            occurred_at=_now(),
            run_id=rid,
            sequence=seq.next(rid),
            payload={"command": f"c{n}"},
        )
        for n in range(n)
    ]


# --- Cursor token round-trip ------------------------------------------------


def test_cursor_round_trips_through_token():
    env = build_event("terminal", occurred_at=_now(), run_id="run_1", sequence=3, payload={"status": "succeeded"})
    cur = ResumeCursor.from_event(env)
    tok = cur.to_token()
    assert isinstance(tok, str)
    restored = ResumeCursor.from_token(tok)
    assert restored == cur
    assert restored.to_token() == tok


def test_cursor_token_rejects_malformed_input():
    # Fail-closed: a truncated/garbage token must not silently parse.
    for bad in ["", "not-json", "[1,2]", '{"run_id":"x"}']:
        with pytest.raises(Exception):
            ResumeCursor.from_token(bad)


def test_no_run_event_cursor_uses_empty_run_boundary():
    env = build_event("warning", occurred_at=_now(), payload={"code": "W", "message": "x"})
    cur = ResumeCursor.from_event(env)
    assert cur.run_id == ""
    assert cur.sequence == -1


# --- events_after: gap-free, duplicate-free tail -----------------------------


def test_events_after_none_returns_all_in_canonical_order():
    s = RunSequencer()
    events = _emit(s, "run_a", 2) + _emit(s, "run_b", 2)
    out = events_after(None, events)
    assert out == sorted(events, key=order_key)


def test_events_after_excludes_delivered_and_earlier_only():
    s = RunSequencer()
    events = _emit(s, "run_a", 3) + _emit(s, "run_b", 2)
    ordered = sorted(events, key=order_key)
    # Cursor at the 2nd delivered event (index 1); tail is everything after it.
    cursor = ResumeCursor.from_event(ordered[1])
    out = events_after(cursor, events)
    # The cursor's own event must NOT be re-included (exclusive boundary).
    assert ordered[1].event_id not in {e.event_id for e in out}
    # Tail is contiguous from index 2 — no gaps.
    assert out == ordered[2:]


def test_events_after_collapses_duplicate_event_ids():
    s = RunSequencer()
    events = _emit(s, "run_a", 3)
    dup = events[0]  # identical event id re-delivered (e.g. at-least-once transport)
    # A fresh dedupe store marks each id on first sight and collapses the dup.
    store = EventDedupeStore()
    out = events_after(None, events + [dup], dedupe=store)
    assert len(out) == 3  # no duplicate UI entry
    assert {e.event_id for e in out} == {e.event_id for e in events}


def test_events_after_is_deterministic_for_same_inputs():
    s = RunSequencer()
    events = _emit(s, "run_a", 2) + _emit(s, "run_b", 2) + _emit(s, "run_a", 1)
    cursor = ResumeCursor.from_event(sorted(events, key=order_key)[1])
    assert events_after(cursor, events) == events_after(cursor, list(events))


def test_reconnect_simulation_no_gaps_no_duplicates():
    s = RunSequencer()
    # One run's stream delivered in two sessions; a single run keeps canonical
    # order aligned with emission order, so a reconnect cursor at the first
    # session's end yields exactly the second session's tail.
    batch1 = _emit(s, "run_a", 3)
    delivered = sorted(batch1, key=order_key)
    cursor = ResumeCursor.from_event(delivered[-1])
    # The dedupe store already holds every batch1 id (rendered in prior session).
    store = EventDedupeStore()
    for e in batch1:
        store.observe(e.event_id)

    # Reconnect: a duplicate of a batch1 id arrives (at-least-once) plus batch2.
    batch2 = _emit(s, "run_a", 2)
    replay_pool = batch1 + batch2  # source-of-truth list contains everything
    tail = events_after(cursor, replay_pool, dedupe=store)

    # Exactly the new events (run_a seq3,4), gap-free and duplicate-free.
    assert {e.event_id for e in tail} == {e.event_id for e in batch2}
    assert tail == sorted(batch2, key=order_key)
    assert len(tail) == len(batch2)


def test_events_after_combined_with_dedupe_store_is_idempotent():
    s = RunSequencer()
    events = _emit(s, "run_a", 3)
    # Consumer already rendered all three in a prior session.
    store = EventDedupeStore()
    for e in events:
        store.observe(e.event_id)

    # On reconnect the same ids are re-sent (at-least-once); cursor is None but
    # the dedupe store absorbs them -> nothing new to render.
    tail = events_after(None, events, dedupe=store)
    assert tail == []
