"""M6.4 slice 6 — Transport seam for a future Redis/multi-worker impl.

Acceptance item 6: "Transport seam supports a future Redis/multi-worker
implementation." The seam is an `EventTransport` Protocol: `publish` appends a
canonical `EventEnvelope` (serialized via `to_wire`), and `read_after(stream,
position)` returns the tail from a stream-position cursor. The in-memory impl is
what ships today (single worker); the Redis impl proves the same canonical
events cross the boundary unchanged (via `to_wire`/`from_wire`) so it can be
swapped in later without touching routes/agents. The redis client is injected
(and lazily imported) so this module loads without `redis` installed.
"""
from __future__ import annotations

from crewspace.dto.events import (
    EventEnvelope,
    EventTransport,
    InMemoryEventTransport,
    RedisEventTransport,
    build_event,
)


def _ev(event_type, run_id="run_1", **kw):
    occurred_at = kw.pop("occurred_at", _now())
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
    payload = kw.pop("payload", payloads[event_type])
    return build_event(event_type, occurred_at=occurred_at, run_id=run_id,
                       payload=payload, **kw)


def _now():
    from datetime import datetime, timezone
    return datetime(2026, 8, 25, 12, 0, 0, tzinfo=timezone.utc)


def test_event_transport_is_a_protocol():
    # Any object with publish/read_after satisfies the seam.
    assert isinstance(InMemoryEventTransport(), EventTransport)


def test_inmemory_publish_then_read_after_returns_tail():
    t = InMemoryEventTransport()
    e1 = _ev("plan")
    e2 = _ev("command")
    e3 = _ev("terminal")
    assert t.publish(e1) == 0
    assert t.publish(e2) == 1
    assert t.publish(e3) == 2
    # read_after(None) -> all
    events, pos = t.read_after("run_1", None)
    assert [e.event_id for e in events] == [e1.event_id, e2.event_id, e3.event_id]
    assert pos == 3
    # resume after delivering up to index 1 (e2) -> only the undelivered tail (e3),
    # no gap and no duplicate of e1/e2
    events, pos = t.read_after("run_1", 1)
    assert [e.event_id for e in events] == [e3.event_id]
    assert pos == 3


def test_inmemory_isolated_per_stream():
    t = InMemoryEventTransport()
    a = _ev("plan", run_id="run_a")
    b = _ev("plan", run_id="run_b")
    t.publish(a)
    t.publish(b)
    events, _ = t.read_after("run_a", None)
    assert [e.event_id for e in events] == [a.event_id]


# --- Minimal fake redis client (append-only list) for the Redis adapter ------


class _FakeRedis:
    def __init__(self):
        self._data: dict[str, list[bytes]] = {}

    def rpush(self, key: str, value: str) -> int:
        self._data.setdefault(key, []).append(value.encode("utf-8"))
        return len(self._data[key])

    def lrange(self, key: str, start: int, end: int) -> list[bytes]:
        items = self._data.get(key, [])
        if end == -1:
            end = len(items)
        return items[start:end]


def test_redis_transport_crosses_boundary_via_wire():
    fake = _FakeRedis()
    t = RedisEventTransport(redis_client=fake, prefix="evt")
    e1 = _ev("plan")
    e2 = _ev("command")
    e3 = _ev("terminal")
    t.publish(e1)
    t.publish(e2)
    t.publish(e3)
    # The same canonical events come back, unchanged by the round trip.
    events, pos = t.read_after("run_1", None)
    assert pos == 3
    assert [e.event_type for e in events] == ["plan", "command", "terminal"]
    assert [e.event_id for e in events] == [e1.event_id, e2.event_id, e3.event_id]
    # payload survives wire (canonical event, not a raw dict)
    assert events[0].payload.summary == e1.payload.summary
    # storage key is namespaced
    assert "evt:run_1" in fake._data


def test_redis_transport_resume_from_cursor_no_gaps():
    fake = _FakeRedis()
    t = RedisEventTransport(redis_client=fake, prefix="evt")
    ids = []
    for et in ("plan", "command", "terminal"):
        ids.append(t.publish(_ev(et)))
    # publish returns 0-based stream positions
    assert ids == [0, 1, 2]
    # resume after delivering up to index 1 (e2) -> only the undelivered tail (e3),
    # no gap (e1/e2 skipped) and no duplicate
    events, pos = t.read_after("run_1", 1)
    assert len(events) == 1
    assert events[0].event_type == "terminal"
    assert pos == 3
