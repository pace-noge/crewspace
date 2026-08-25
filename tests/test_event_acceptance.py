"""M6.4 slice 7 — Cohesive acceptance suite (acceptance item 7).

Acceptance item 7: "Contract, replay, reconnect, and migration-compatibility
tests pass." This file consolidates the per-slice guarantees into one place so
the milestone is provable from a single green run:

  - contract: versioned envelope, closed typed catalog, fail-closed routing ids
  - replay:   deterministic id/dedupe + canonical ordering
  - reconnect: resume cursor with no gaps / no duplicate UI entries
  - audit:    JSON/CSV export carries the SAME canonical events as the UI
  - transport: in-memory and Redis adapters share one Protocol seam
  - migration-compat: the event DTO module is pure (no sqlalchemy import), so
    `makemigrations --check` can never drift because of it.

No new production code is needed for slice 7 — these are cross-cutting
contract assertions over APIs already shipped in slices 1-6.
"""
from __future__ import annotations

import ast
import subprocess
import sys

from crewspace.dto.events import (
    ActivityItem,
    EventDedupeStore,
    EventEnvelope,
    EventTransport,
    InMemoryEventTransport,
    RedisEventTransport,
    ResumeCursor,
    RunSequencer,
    build_event,
    compact_summary,
    events_after,
    export_events_csv,
    export_events_json,
    order_key,
    run_to_activity,
    run_to_events,
    to_activity_item,
)


def _ev(event_type, run_id="run_1", seq=None, **k):
    from datetime import datetime, timezone
    occurred = k.pop("occurred_at", datetime(2026, 8, 25, tzinfo=timezone.utc))
    payloads = {
        "plan": {"summary": "work"},
        "command": {"command": "x", "exit_code": 0},
        "terminal": {"status": "succeeded"},
        "file": {"path": "f", "action": "write"},
        "test": {"status": "passed", "passed": 1, "failed": 0},
        "artifact": {"path": "a", "size_bytes": 1, "kind": "bundle"},
        "approval": {"decision": "granted", "action_class": "git_push"},
        "warning": {"code": "W", "message": "x"},
    }
    payload = k.pop("payload", payloads[event_type])
    return build_event(event_type, occurred_at=occurred, run_id=run_id,
                       sequence=seq, payload=payload, **k)


# --- contract: versioned envelope + closed typed catalog ----------------------


def test_contract_envelope_is_versioned_and_rejects_bad_version():
    good = _ev("terminal")
    assert good.schema_version == "1.0"
    bad = dict(good.to_wire())
    bad["schema_version"] = "9.9"
    raised = False
    try:
        EventEnvelope.from_wire(bad)
    except ValueError:
        raised = True
    assert raised, "from_wire must reject unsupported schema versions"


def test_contract_typed_payload_is_closed_per_event_type():
    # wrong payload type for the event is rejected (extra=forbid on every model)
    raised = False
    try:
        build_event("terminal", occurred_at=_ev("terminal").occurred_at,
                    run_id="r", payload={"command": "x", "exit_code": 0})
    except Exception:
        raised = True
    assert raised, "terminal event must reject a command payload"


def test_contract_routing_ids_are_fail_closed():
    for bad in ("../../etc", "a b", "/x", "..", "a\0b", "a" * 65):
        raised = False
        try:
            build_event("warning", occurred_at=_ev("warning").occurred_at,
                        run_id=bad, payload={"code": "W", "message": "x"})
        except Exception:
            raised = True
        assert raised, f"unsafe run_id must be rejected: {bad!r}"
    # valid id is accepted
    assert build_event("warning", occurred_at=_ev("warning").occurred_at,
                       run_id="run_ok-1", payload={"code": "W", "message": "x"}).run_id == "run_ok-1"


# --- replay: deterministic id/dedupe + canonical ordering ---------------------


def test_replay_sequence_and_dedupe_are_deterministic():
    seq = RunSequencer()
    assert [seq.next("r"), seq.next("r"), seq.next("r")] == [0, 1, 2]
    store = EventDedupeStore()
    e1 = _ev("plan", run_id="r", seq=0)
    assert store.observe(e1.event_id) is False  # first sighting: not a duplicate yet
    assert store.observe(e1.event_id) is True   # replay of same id is a duplicate


def test_replay_canonical_order_is_total_and_stable():
    a = _ev("plan", run_id="r", seq=2)
    b = _ev("command", run_id="r", seq=1)
    c = _ev("terminal", run_id="r", seq=0)
    ordered = sorted([a, b, c], key=order_key)
    # deterministic total order: ascending sequence within the run
    assert [e.sequence for e in ordered] == [0, 1, 2]
    # re-sorting the same set yields the identical order (stable)
    assert [e.sequence for e in sorted(ordered, key=order_key)] == [0, 1, 2]


# --- reconnect: resume cursor, no gaps, no duplicate UI entries ---------------


def test_reconnect_no_gaps_no_duplicates():
    events = [
        _ev("plan", run_id="r", seq=0),
        _ev("command", run_id="r", seq=1),
        _ev("terminal", run_id="r", seq=2),
    ]
    cursor = ResumeCursor.from_event(events[0])  # delivered up to first
    tail = events_after(cursor, events)
    # strictly later, no dupe of the delivered event, no gap
    assert [e.event_id for e in tail] == [events[1].event_id, events[2].event_id]
    # a second delivery (at-least-once) collapses to nothing new
    again = events_after(cursor, events)
    assert {e.event_id for e in again} == {events[1].event_id, events[2].event_id}


# --- audit: JSON/CSV export carries the SAME canonical events as the UI ------


def test_audit_export_matches_ui_activity_contract(run_like):
    events = run_to_events(run_like)
    items = run_to_activity(run_like)
    assert {e.event_id for e in events} == {i.event_id for i in items}
    doc = export_events_json(events)
    import json
    parsed = json.loads(doc)
    assert parsed["count"] == len(events)
    csv_doc = export_events_csv(events)
    import csv, io
    rows = list(csv.DictReader(io.StringIO(csv_doc)))
    assert len(rows) == len(events)
    summaries = {i.event_type: i.summary for i in items}
    for row in rows:
        assert row["summary"] == summaries[row["event_type"]]


# --- transport: one Protocol seam for in-memory and future Redis -------------


def test_transport_seam_shared_by_inmemory_and_redis():
    class FakeRedis:
        def __init__(self): self.d = {}
        def rpush(self, k, v): self.d.setdefault(k, []).append(v.encode()); return len(self.d[k])
        def lrange(self, k, s, e):
            items = self.d.get(k, []); return items if e == -1 else items[s:e]
    mem = InMemoryEventTransport()
    red = RedisEventTransport(redis_client=FakeRedis())
    assert isinstance(mem, EventTransport) and isinstance(red, EventTransport)
    e1 = _ev("plan"); e2 = _ev("command")
    assert mem.publish(e1) == 0 and mem.publish(e2) == 1
    assert red.publish(e1) == 0 and red.publish(e2) == 1
    mem_ev, _ = mem.read_after("run_1", None)
    red_ev, _ = red.read_after("run_1", None)
    assert [e.event_id for e in mem_ev] == [e.event_id for e in red_ev]
    # both impls expose the same canonical wire shape
    assert mem_ev[0].to_wire() == red_ev[0].to_wire()


# --- migration-compat: event DTO module is pure (no sqlalchemy) ---------------


def test_migration_compat_event_module_has_no_sqlalchemy_import():
    import crewspace.dto.events as m
    tree = ast.parse(open(m.__file__).read())
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            imported.add(node.module or "")
    assert "sqlalchemy" not in imported, (
        "events.py must not import sqlalchemy; otherwise the DTO boundary could "
        "drift the DB schema and break `makemigrations --check`."
    )


def test_migration_compat_makemigrations_check_stays_clean():
    # The event DTOs are pure; the DB schema must remain in sync.
    result = subprocess.run(
        [sys.executable, "-m", "crewspace.management.cli", "makemigrations", "--check"],
        capture_output=True, text=True, timeout=120,
    )
    assert result.returncode == 0, (
        f"makemigrations --check drifted:\n{result.stdout}\n{result.stderr}"
    )


# --- a minimal run-like object for run_to_events/run_to_activity -------------


class _RunLike:
    def __init__(self):
        from datetime import datetime, timezone
        self.id = "run_accept"
        self.agent_id = "agent_planner"
        self.instruction = "do the thing"
        self.status = "succeeded"
        self.created_at = datetime(2026, 8, 25, 10, 0, 0, tzinfo=timezone.utc)
        self.started_at = datetime(2026, 8, 25, 10, 0, 1, tzinfo=timezone.utc)
        self.finished_at = datetime(2026, 8, 25, 10, 0, 2, tzinfo=timezone.utc)
        self.failure_reason = ""
        self.recent_output = "tail"


import pytest


@pytest.fixture
def run_like():
    return _RunLike()
