"""M6.4 slice 2 — Deterministic per-run sequence/order and event-id dedupe.

Acceptance item 2: "Per-run sequence/order and event-id dedupe are deterministic."

These are the transport-primitive contracts (in-process now; a future
Redis/multi-worker implementation must satisfy the same protocol). They must be
pure-DTO-layer (no DB/websocket import) and deterministic: the same emission
pattern for a run always yields the same sequence numbers, and an event id is
either unseen or seen idempotently.
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from crewspace.dto.events import (
    EventDedupeStore,
    EventEnvelope,
    RunSequencer,
    build_event,
    new_event_id,
    order_key,
)


def _now() -> datetime:
    return datetime(2026, 8, 25, 10, 0, 0, tzinfo=timezone.utc)


def _emit(seq: RunSequencer, run_id: str, event_type: str = "command") -> EventEnvelope:
    return build_event(
        event_type,
        occurred_at=_now(),
        run_id=run_id,
        sequence=seq.next(run_id),
        payload={"command": "x"},
    )


# --- RunSequencer: deterministic per-run ordering -----------------------------


def test_run_sequencer_monotonic_within_a_run():
    s = RunSequencer()
    seqs = [s.next("run_1") for _ in range(3)]
    assert seqs == [0, 1, 2]


def test_run_sequencer_is_independent_per_run():
    s = RunSequencer()
    s.next("run_1")  # 0
    s.next("run_1")  # 1
    assert s.next("run_2") == 0  # separate run restarts at 0
    assert s.next("run_1") == 2  # run_1 continues


def test_run_sequencer_is_deterministic_for_same_emission_pattern():
    # Two fresh sequencers fed the same interleaved pattern must produce
    # identical sequences — determinism is the whole point of item 2.
    def pattern() -> list[tuple[str, int]]:
        sq = RunSequencer()
        out: list[tuple[str, int]] = []
        for rid in ["run_a", "run_b", "run_a", "run_b", "run_a"]:
            out.append((rid, sq.next(rid)))
        return out

    assert pattern() == pattern()


def test_run_sequencer_peek_does_not_advance():
    s = RunSequencer()
    s.next("run_1")
    assert s.peek("run_1") == 1
    assert s.peek("run_1") == 1
    assert s.next("run_1") == 1


def test_run_sequencer_reset_clears_one_run_or_all():
    s = RunSequencer()
    s.next("run_1")
    s.next("run_2")
    s.reset("run_1")
    assert s.next("run_1") == 0
    assert s.next("run_2") == 1  # untouched
    s.reset()
    assert s.next("run_2") == 0


# --- EventDedupeStore: deterministic event-id dedupe --------------------------


def test_dedupe_first_observation_is_unseen_then_seen():
    store = EventDedupeStore()
    eid = new_event_id()
    assert store.observe(eid) is False  # first time: not previously seen
    assert store.observe(eid) is True   # second time: duplicate
    assert store.seen(eid) is True


def test_dedupe_distinct_ids_never_collide():
    store = EventDedupeStore()
    a, b = new_event_id(), new_event_id()
    assert store.observe(a) is False
    assert store.observe(b) is False
    assert store.seen(a) and store.seen(b)


def test_dedupe_reset_forgets_seen_ids():
    store = EventDedupeStore()
    eid = new_event_id()
    store.observe(eid)
    store.reset()
    assert store.observe(eid) is False


# --- order_key: stable replay/resume ordering ---------------------------------


def test_order_key_groups_by_run_then_sequence():
    s = RunSequencer()
    run_a_0 = _emit(s, "run_a")
    run_b_0 = _emit(s, "run_b")
    run_a_1 = _emit(s, "run_a")
    unordered = [run_b_0, run_a_1, run_a_0]
    ordered = sorted(unordered, key=order_key)
    assert ordered == [run_a_0, run_a_1, run_b_0]


def test_order_key_falls_back_to_event_id_without_run():
    no_run_1 = build_event("warning", occurred_at=_now(), payload={"code": "W", "message": "x"})
    no_run_2 = build_event("warning", occurred_at=_now(), payload={"code": "W", "message": "x"})
    # different event ids -> distinct, stable, total order keys
    assert order_key(no_run_1) != order_key(no_run_2)
    # sorting by order_key is deterministic and total: the larger id sorts last.
    lo, hi = sorted([no_run_1, no_run_2], key=lambda e: e.event_id)
    assert sorted([no_run_2, no_run_1], key=order_key) == sorted([no_run_1, no_run_2], key=order_key)
    # The two no-run events bracket any run-bearing event (run_id sorts AFTER
    # the empty no-run bucket: "" < "r1").
    with_run = build_event("command", occurred_at=_now(), run_id="r1", sequence=0, payload={"command": "x"})
    mixed = sorted([no_run_1, no_run_2, with_run], key=order_key)
    assert mixed[-1] is with_run  # run-bearing sorts after no-run ones
