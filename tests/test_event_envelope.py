"""M6.4 slice 1 — Typed execution events: versioned envelope + typed event catalog.

Contract-level tests only (no app/DB fixture): the envelope and the typed event
catalog are pure DTOs at the application<->api boundary, so they must validate
and serialize deterministically without any storage seam.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from crewspace.dto.events import (
    EVENT_CATALOG,
    EventEnvelope,
    EventType,
    build_event,
    new_event_id,
)


def _now() -> datetime:
    return datetime(2026, 8, 25, 9, 0, 0, tzinfo=timezone.utc)


# --- EventType catalog -------------------------------------------------------


def test_event_type_catalog_covers_plan_file_command_test_artifact_approval_warning_terminal():
    expected = {
        "plan",
        "file",
        "command",
        "test",
        "artifact",
        "approval",
        "warning",
        "terminal",
    }
    assert expected.issubset(set(EventType.__args__))  # type: ignore[attr-defined]
    # The catalog maps every supported event type to its typed payload model.
    for name in expected:
        assert name in EVENT_CATALOG


# --- Versioned envelope ------------------------------------------------------


def test_envelope_rejects_unknown_schema_version():
    with pytest.raises(ValidationError):
        EventEnvelope(
            schema_version="2.0",
            event_id=new_event_id(),
            event_type="terminal",
            occurred_at=_now(),
            payload={"status": "succeeded"},
        )


def test_envelope_forbids_extra_top_level_fields():
    with pytest.raises(ValidationError):
        EventEnvelope(
            schema_version="1.0",
            event_id=new_event_id(),
            event_type="terminal",
            occurred_at=_now(),
            payload={"status": "succeeded"},
            surprise="nope",  # type: ignore[arg-type]
        )


def test_envelope_carries_required_routing_fields():
    env = build_event(
        "terminal",
        occurred_at=_now(),
        actor_id="agent_planner",
        channel_id="chan_general",
        run_id="run_1",
        correlation_id="corr_1",
        payload={"status": "succeeded"},
    )
    assert env.schema_version == "1.0"
    assert env.event_type == "terminal"
    assert env.actor_id == "agent_planner"
    assert env.channel_id == "chan_general"
    assert env.run_id == "run_1"
    assert env.correlation_id == "corr_1"
    assert env.sequence is None
    # payload is the typed TerminalEvent model, not a raw dict.
    assert env.payload.status == "succeeded"


def test_envelope_allows_absent_optional_routing_fields():
    env = build_event(
        "warning", occurred_at=_now(), payload={"code": "W001", "message": "x"}
    )
    assert env.actor_id is None
    assert env.channel_id is None
    assert env.run_id is None
    assert env.correlation_id is None
    assert env.sequence is None
    assert env.payload.code == "W001"


# --- Typed payload enforcement -----------------------------------------------


def test_build_terminal_event_validates_against_typed_payload():
    env = build_event(
        "terminal",
        occurred_at=_now(),
        payload={"status": "failed", "reason": "boom"},
    )
    assert env.payload.status == "failed"
    assert env.payload.reason == "boom"


def test_build_terminal_event_rejects_bad_status():
    with pytest.raises(ValidationError):
        build_event(
            "terminal", occurred_at=_now(), payload={"status": "exploded"}
        )


@pytest.mark.parametrize(
    "bad_id",
    ["../../etc", "a b", "a\tb", "/x", "a.b", "..", "", "a/b/c"],
)
def test_envelope_rejects_unsafe_routing_ids(bad_id):
    # The SafeId pattern is anchored, so traversal/space/control/absolute ids
    # fail closed rather than being accepted by a substring match.
    with pytest.raises(ValidationError):
        EventEnvelope(
            schema_version="1.0",
            event_id=new_event_id(),
            event_type="terminal",
            occurred_at=_now(),
            actor_id=bad_id,
            payload={"status": "succeeded"},
        )


def test_build_event_rejects_payload_for_wrong_event_type():
    # A 'file' payload must not satisfy a 'terminal' event.
    with pytest.raises(ValidationError):
        build_event(
            "terminal",
            occurred_at=_now(),
            payload={"path": "main.py", "action": "write"},
        )


def test_build_event_rejects_unknown_payload_field():
    with pytest.raises(ValidationError):
        build_event(
            "terminal",
            occurred_at=_now(),
            payload={"status": "succeeded", "bogus": 1},  # type: ignore[dict-item]
        )


# --- Deterministic / canonical serialization (dedupe + audit + export) -------


def test_event_id_is_unique_across_calls():
    assert new_event_id() != new_event_id()


def test_canonical_json_is_lexicographically_stable():
    eid = new_event_id()
    env = build_event(
        "command",
        event_id=eid,
        occurred_at=_now(),
        run_id="run_1",
        payload={"command": "pytest", "exit_code": 0},
    )
    first = env.canonical_json()
    # Rebuilding with identical inputs yields identical canonical bytes.
    env2 = build_event(
        "command",
        event_id=eid,
        occurred_at=_now(),
        run_id="run_1",
        payload={"command": "pytest", "exit_code": 0},
    )
    assert env2.canonical_json() == first
    # The canonical form is valid JSON with the expected key set, sort-stable.
    parsed = json.loads(first)
    assert parsed["event_id"] == eid
    assert parsed["event_type"] == "command"
    assert json.dumps(parsed, sort_keys=True, separators=(",", ":")) == first


def test_dedupe_key_uses_event_id():
    env = build_event("test", occurred_at=_now(), payload={"status": "passed"})
    assert env.dedupe_key == env.event_id


# --- Transport seam (future Redis / multi-worker) ----------------------------


def test_envelope_round_trips_through_wire_dict():
    env = build_event(
        "artifact",
        occurred_at=_now(),
        actor_id="agent_planner",
        payload={"path": "dist/app.zip", "size_bytes": 1024, "kind": "bundle"},
    )
    wire = env.to_wire()
    assert isinstance(wire, dict)
    restored = EventEnvelope.from_wire(wire)
    assert restored == env
    assert restored.canonical_json() == env.canonical_json()
