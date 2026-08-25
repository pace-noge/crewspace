"""Typed execution events and the unified event envelope (M6.4 slice 1).

This is the contract layer for Crewspace execution telemetry: a single versioned
`EventEnvelope` that wraps a *typed* `payload` discriminated by `event_type`.

The envelope is intentionally storage-agnostic. Nothing here imports the DB, the
models, or the websocket layer: it is pure DTO at the application<->api boundary,
so the same object can be (a) broadcast over the channel/control-plane socket,
(b) persisted for replay/resume, (c) exported to JSON/CSV audit, and (d) rebuilt
from a Redis stream in a future multi-worker deployment. Acceptance items 1 of
M6.4 ("versioned schemas exist for envelope and initial typed event catalog")
and 2 ("per-run sequence/order and event-id dedupe are deterministic") start here;
the deterministic `event_id`, `canonical_json`, and `dedupe_key` are the building
blocks the replay/resume slice will build on.
"""
from __future__ import annotations

import csv
import io
import json
import secrets
import uuid
from datetime import datetime
from typing import Annotated, Any, Literal, Union

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

# Bounded, traversal-free identifiers (reuse the same safety bar as change_sets).
# Anchored so pydantic's pattern match (re.search) is equivalent to fullmatch —
# an unanchored pattern would accept a *substring* of a traversal id like
# '../../etc' (matches 'etc'), defeating fail-closed routing-context checks.
SafeId = Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")]
BoundedText = Annotated[str, StringConstraints(max_length=8192)]
ShortText = Annotated[str, StringConstraints(min_length=1, max_length=256)]

# The only envelope schema version we emit today. `from_wire` rejects anything
# else so a future major version cannot be silently misread by old consumers.
CURRENT_SCHEMA_VERSION = "1.0"
SUPPORTED_SCHEMA_VERSIONS = frozenset({CURRENT_SCHEMA_VERSION})


def new_event_id() -> str:
    """Generate a globally-unique, collision-resistant event id.

    UUIDv4 + a random secret suffix keeps ids unique across workers without a
    central sequence source, which is the property the dedupe/replay slice needs.
    """
    return f"evt_{uuid.uuid4().hex}{secrets.token_hex(8)}"


# --- Typed event payloads ----------------------------------------------------
#
# Each event_type maps to exactly one payload model in EVENT_CATALOG below.
# These are deliberately small, explicit contracts — add new fields as slices
# consume them, and keep `extra="forbid"` so producers cannot slip in ad-hoc keys.


class PlanEvent(BaseModel):
    model_config = ConfigDict(extra="forbid")
    summary: BoundedText
    step: ShortText | None = None
    total_steps: int | None = Field(default=None, ge=0)


class FileEvent(BaseModel):
    model_config = ConfigDict(extra="forbid")
    path: BoundedText
    action: Literal["created", "read", "write", "edit", "delete", "rename"]
    bytes_written: int | None = Field(default=None, ge=0)


class CommandEvent(BaseModel):
    model_config = ConfigDict(extra="forbid")
    command: BoundedText
    exit_code: int | None = None
    timed_out: bool = False


class TestEvent(BaseModel):
    model_config = ConfigDict(extra="forbid")
    status: Literal["passed", "failed", "skipped", "running"]
    name: ShortText | None = None
    passed: int | None = Field(default=None, ge=0)
    failed: int | None = Field(default=None, ge=0)


class ArtifactEvent(BaseModel):
    model_config = ConfigDict(extra="forbid")
    path: BoundedText
    size_bytes: int = Field(ge=0)
    kind: ShortText | None = None


class ApprovalEvent(BaseModel):
    model_config = ConfigDict(extra="forbid")
    decision: Literal["requested", "granted", "denied", "expired"]
    action_class: ShortText
    scope: ShortText | None = None
    principal_id: SafeId | None = None


class WarningEvent(BaseModel):
    model_config = ConfigDict(extra="forbid")
    code: ShortText
    message: BoundedText
    severity: Literal["info", "warning", "error"] = "warning"


class TerminalEvent(BaseModel):
    model_config = ConfigDict(extra="forbid")
    status: Literal[
        "succeeded", "failed", "cancelled", "timed_out", "interrupted"
    ]
    reason: BoundedText | None = None
    duration_ms: int | None = Field(default=None, ge=0)


# Discriminated union so `payload` is validated against the model that matches
# `event_type`. This is what makes build_event reject a wrong-type payload.
_EVENT_UNION = Union[
    PlanEvent,
    FileEvent,
    CommandEvent,
    TestEvent,
    ArtifactEvent,
    ApprovalEvent,
    WarningEvent,
    TerminalEvent,
]

EVENT_CATALOG: dict[str, type[BaseModel]] = {
    "plan": PlanEvent,
    "file": FileEvent,
    "command": CommandEvent,
    "test": TestEvent,
    "artifact": ArtifactEvent,
    "approval": ApprovalEvent,
    "warning": WarningEvent,
    "terminal": TerminalEvent,
}

# Order matters for Literal generation: keep aligned with EVENT_CATALOG keys.
EventType = Literal[
    "plan",
    "file",
    "command",
    "test",
    "artifact",
    "approval",
    "warning",
    "terminal",
]


class EventEnvelope(BaseModel):
    """Versioned, typed execution event.

    `schema_version` lets old consumers reject futures they cannot understand.
    `payload` is a discriminated union keyed by `event_type`, so a malformed or
    mismatched payload fails validation at the boundary rather than at the sink.
    """

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["1.0"] = CURRENT_SCHEMA_VERSION  # type: ignore[valid-type]
    event_id: str
    event_type: EventType
    occurred_at: datetime
    actor_id: SafeId | None = None
    channel_id: SafeId | None = None
    run_id: SafeId | None = None
    correlation_id: SafeId | None = None
    sequence: int | None = Field(default=None, ge=0)
    payload: _EVENT_UNION

    @classmethod
    def from_wire(cls, data: dict[str, Any]) -> "EventEnvelope":
        """Parse a wire dict, rejecting unsupported schema versions."""
        version = data.get("schema_version")
        if version not in SUPPORTED_SCHEMA_VERSIONS:
            raise ValueError(
                f"unsupported event schema_version: {version!r} "
                f"(supported: {sorted(SUPPORTED_SCHEMA_VERSIONS)})"
            )
        return cls.model_validate(data)

    def to_wire(self) -> dict[str, Any]:
        """Serialize to a plain dict for transport/storage."""
        return self.model_dump(mode="json")

    def canonical_json(self) -> str:
        """Sort-stable canonical bytes used for dedupe, audit, and export.

        `sort_keys=True` makes identical events byte-identical across workers and
        languages; `event_id` is included so this doubles as the canonical record.
        """
        return json.dumps(self.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))

    @property
    def dedupe_key(self) -> str:
        """Stable key for in-flight dedupe (and future resume cursors).

        Event ids are unique by construction, so the id itself is the safe key;
        a consumer that wants per-run ordering should combine `run_id`+`sequence`.
        """
        return self.event_id


def build_event(
    event_type: str,
    *,
    occurred_at: datetime,
    payload: dict[str, Any],
    event_id: str | None = None,
    actor_id: str | None = None,
    channel_id: str | None = None,
    run_id: str | None = None,
    correlation_id: str | None = None,
    sequence: int | None = None,
) -> EventEnvelope:
    """Construct a validated envelope, dispatching payload to its typed model.

    Raises pydantic.ValidationError if `event_type` is unknown or `payload` does
    not satisfy the matching typed model. `actor_id`/`channel_id`/`run_id`/
    `correlation_id` are bound to the SafeId pattern (rejects traversal/control
    chars), so unauthorized or malformed routing context fails closed at the edge.
    """
    if event_type not in EVENT_CATALOG:
        raise ValueError(f"unknown event_type: {event_type!r}")
    # Validate the typed payload first so the error points at the payload, not
    # the whole envelope.
    EVENT_CATALOG[event_type].model_validate(payload)
    return EventEnvelope(
        event_id=event_id or new_event_id(),
        event_type=event_type,  # type: ignore[arg-type]
        occurred_at=occurred_at,
        actor_id=actor_id,  # type: ignore[arg-type]
        channel_id=channel_id,  # type: ignore[arg-type]
        run_id=run_id,  # type: ignore[arg-type]
        correlation_id=correlation_id,  # type: ignore[arg-type]
        sequence=sequence,
        payload=payload,  # type: ignore[arg-type]
    )


# --- Deterministic ordering & dedupe primitives (acceptance item 2) ------------
#
# These are the transport-agnostic building blocks for ordered replay/resume and
# cross-reconnect dedupe. They are deliberately in-process and pure; a future
# Redis/multi-worker implementation must satisfy the SAME protocol (per-run
# monotonic sequence, unseen->seen event-id transitions, stable order_key).


class RunSequencer:
    """Per-run monotonic sequence counter.

    Each run gets its own independent counter starting at 0. The sequence is the
    deterministic per-run ordering key replay/resume uses, so the same emission
    pattern for a run always yields the same numbers regardless of process or
    worker — that determinism is the contract (see test_run_sequencer_*).
    """

    def __init__(self) -> None:
        self._seq: dict[str, int] = {}

    def next(self, run_id: str) -> int:
        n = self._seq.get(run_id, 0)
        self._seq[run_id] = n + 1
        return n

    def peek(self, run_id: str) -> int:
        return self._seq.get(run_id, 0)

    def reset(self, run_id: str | None = None) -> None:
        if run_id is None:
            self._seq.clear()
        else:
            self._seq.pop(run_id, None)


class EventDedupeStore:
    """Idempotent event-id membership for dedupe across reconnects/replays.

    `observe` returns False the first time an id is seen and True thereafter, so
    a consumer can drop duplicates without re-processing. The store is bounded
    by the caller (it is a pure primitive); persistent storage belongs to the
    persistence slice.
    """

    def __init__(self) -> None:
        self._seen: set[str] = set()

    def observe(self, event_id: str) -> bool:
        if event_id in self._seen:
            return True
        self._seen.add(event_id)
        return False

    def seen(self, event_id: str) -> bool:
        return event_id in self._seen

    def reset(self, event_id: str | None = None) -> None:
        if event_id is None:
            self._seen.clear()
        else:
            self._seen.discard(event_id)


def order_key(env: EventEnvelope) -> tuple[str, int, str]:
    """Stable total order for replay/resume and de-duplicated rendering.

    Groups by run (so a run's events sort together by sequence) and breaks ties
    within and across runs by event_id for a guaranteed total order. Events
    without a run_id sort first and are ordered solely by event_id.
    """
    return (env.run_id or "", env.sequence if env.sequence is not None else -1, env.event_id)


# --- Resume cursor (acceptance item 3) ----------------------------------------
#
# Reconnect must resume from a cursor without gaps or duplicate UI entries. The
# cursor is POSITIONAL over the canonical total order (order_key), not a list
# index — so it stays valid across batches and re-sorts. It encodes the
# last-delivered boundary as an opaque, serializable token and is fail-closed:
# a malformed token raises rather than silently returning the whole stream.


class ResumeCursor:
    """Position in the canonical order after which the client resumes.

    The boundary is (run_id, sequence, event_id). run_id=="" / sequence==-1
    denotes the synthetic head before any no-run event.
    """

    __slots__ = ("run_id", "sequence", "event_id")

    def __init__(self, run_id: str, sequence: int, event_id: str) -> None:
        self.run_id = run_id
        self.sequence = sequence
        self.event_id = event_id

    @classmethod
    def from_event(cls, env: EventEnvelope) -> "ResumeCursor":
        return cls(
            run_id=env.run_id or "",
            sequence=env.sequence if env.sequence is not None else -1,
            event_id=env.event_id,
        )

    @classmethod
    def from_token(cls, token: str) -> "ResumeCursor":
        try:
            data = json.loads(token)
        except (ValueError, TypeError) as exc:
            raise ValueError("malformed resume token") from exc
        if not isinstance(data, dict):
            raise ValueError("resume token must be a JSON object")
        if not all(k in data for k in ("run_id", "sequence", "event_id")):
            raise ValueError("resume token missing required fields")
        run_id = data["run_id"]
        sequence = data["sequence"]
        event_id = data["event_id"]
        if not isinstance(run_id, str) or not isinstance(event_id, str):
            raise ValueError("resume token has wrong field types")
        if not isinstance(sequence, int) or isinstance(sequence, bool):
            raise ValueError("resume token has wrong sequence type")
        return cls(run_id=run_id, sequence=sequence, event_id=event_id)

    def to_token(self) -> str:
        return json.dumps(
            {"run_id": self.run_id, "sequence": self.sequence, "event_id": self.event_id},
            sort_keys=True,
            separators=(",", ":"),
        )

    def __eq__(self, other: object) -> bool:
        return (
            isinstance(other, ResumeCursor)
            and self.run_id == other.run_id
            and self.sequence == other.sequence
            and self.event_id == other.event_id
        )

    def __repr__(self) -> str:  # pragma: no cover - debug aid
        return f"ResumeCursor(run_id={self.run_id!r}, sequence={self.sequence}, event_id={self.event_id!r})"


def events_after(
    cursor: ResumeCursor | None,
    events: list[EventEnvelope],
    *,
    dedupe: EventDedupeStore | None = None,
) -> list[EventEnvelope]:
    """Return the gap-free, duplicate-free tail after ``cursor`` in order_key order.

    - Excludes the cursor's own event (exclusive upper boundary) and everything
      ordered before it, so the tail is contiguous (no gaps).
    - Dedupes by event_id; if ``dedupe`` is supplied, observed ids are marked
      and previously-seen ids are skipped (idempotent across reconnects).
    - Deterministic given the same inputs: sorts a fresh copy, never mutates.

    Items sent before but whose event_id is not in the cursor's past are still
    included (their position could not be known from the boundary); the caller's
    ``dedupe`` store is the source of truth for already-rendered ids.
    """
    ordered = sorted(events, key=order_key)
    start = 0
    if cursor is not None:
        # Index of the first event strictly after the cursor boundary.
        for i, env in enumerate(ordered):
            if order_key(env) > (cursor.run_id, cursor.sequence, cursor.event_id):
                start = i
                break
        else:
            # Cursor is at or beyond the end: nothing new.
            start = len(ordered)
    tail = ordered[start:]
    if dedupe is None:
        return tail
    result: list[EventEnvelope] = []
    for env in tail:
        if dedupe.observe(env.event_id):
            continue
        result.append(env)
    return result


# --- Compact activity rendering (acceptance item 4) ---------------------------
#
# The UI renders a compact row per execution event plus the raw payload on
# demand. `to_activity_item` maps an `EventEnvelope` to a frozen, UI-shaped
# `ActivityItem`; `compact_summary` produces the one-line label. These are pure
# DTOs (no template/db coupling) so they are unit-testable without a browser.


class _Frozen(BaseModel):
    """Frozen, no-ad-hoc-keys base so the UI contract cannot silently drift."""

    model_config = ConfigDict(frozen=True, extra="forbid")


class ActivityItem(_Frozen):
    event_id: str
    event_type: EventType
    occurred_at: datetime
    kind: str
    summary: str
    actor_id: SafeId | None = None
    run_id: SafeId | None = None
    correlation_id: SafeId | None = None
    sequence: int | None = Field(default=None, ge=0)
    raw: dict[str, Any]


# One-line compact labels per event type. Bounded so the UI row stays short
# (COMPACT_MAX); the full payload is always available in `ActivityItem.raw` on
# demand, so truncating the label never loses information.
COMPACT_MAX = 120


def _trunc(text: str) -> str:
    if len(text) <= COMPACT_MAX:
        return text
    return text[: COMPACT_MAX - 1].rstrip() + "…"


def compact_summary(env: EventEnvelope) -> str:
    p = env.payload
    et = env.event_type
    if et == "plan":
        label = f"plan: {p.summary}".strip()
    elif et == "file":
        label = f"file {p.path}: {p.action}"
    elif et == "command":
        label = f"ran {p.command} (exit {p.exit_code})" if p.exit_code is not None else f"command {p.command}"
    elif et == "test":
        label = (
            f"tests passed {p.passed}/{p.passed + p.failed}"
            if p.passed is not None and p.failed is not None
            else f"test {p.status}"
        )
    elif et == "artifact":
        label = f"artifact {p.path} ({p.size_bytes} B)"
    elif et == "approval":
        label = f"approval {p.decision}: {p.action_class}"
    elif et == "warning":
        label = f"warning {p.code}: {p.message}"
    elif et == "terminal":
        label = f"terminal: {p.status}"
    else:
        label = et  # pragma: no cover - exhaustive union
    return _trunc(label)


def to_activity_item(env: EventEnvelope) -> ActivityItem:
    return ActivityItem(
        event_id=env.event_id,
        event_type=env.event_type,
        occurred_at=env.occurred_at,
        kind=env.event_type,
        summary=compact_summary(env),
        actor_id=env.actor_id,
        run_id=env.run_id,
        correlation_id=env.correlation_id,
        sequence=env.sequence,
        raw=env.payload.model_dump(mode="json"),
    )


def run_to_events(run: Any) -> list[EventEnvelope]:
    """Derive the canonical events a coding run emits from its real fields.

    Produces plan/instruction, started, and terminal `EventEnvelope`s. This is
    the single source of truth both the UI activity stream and the audit export
    consume, so they cannot diverge. Event ids are DETERMINISTIC per
    (run_id, event_type) so the activity view and the audit export always show
    the same canonical events (required by acceptance item 5); they remain
    unique across runs because run_id is unique. Nothing here fabricates output
    — every field comes from the run entity.
    """
    rid = getattr(run, "id", None)
    actor = getattr(run, "agent_id", None)
    created = getattr(run, "created_at", None)
    started = getattr(run, "started_at", None)
    finished = getattr(run, "finished_at", None)
    instruction = getattr(run, "instruction", "") or ""
    status = getattr(run, "status", "") or ""
    failure_reason = getattr(run, "failure_reason", "") or ""

    envelopes: list[EventEnvelope] = []
    if created is not None:
        envelopes.append(
            build_event("plan", occurred_at=created, run_id=rid, actor_id=actor,
                        event_id=f"evt_{rid}_plan",
                        payload={"summary": instruction or "(no instruction)"})
        )
    if started is not None:
        envelopes.append(
            build_event("command", occurred_at=started, run_id=rid, actor_id=actor,
                        event_id=f"evt_{rid}_cmd",
                        payload={"command": "execute run", "exit_code": None})
        )
    if finished is not None:
        payload: dict[str, Any] = {"status": status}
        if status == "failed" and failure_reason:
            payload["reason"] = failure_reason
        envelopes.append(
            build_event("terminal", occurred_at=finished, run_id=rid, actor_id=actor,
                        event_id=f"evt_{rid}_term",
                        payload=payload)
        )
    return envelopes


def run_to_activity(run: Any) -> list[ActivityItem]:
    """Derive compact typed activity for a coding run from its real fields.

    Maps the canonical events (via `run_to_events`) to `ActivityItem`s in
    lifecycle order (time + event-type priority). This is the source-of-truth
    the UI renders; the raw `recent_output` is surfaced separately as on-demand
    "raw logs". Nothing here fabricates output — every field comes from the run
    entity.
    """
    envelopes = run_to_events(run)
    # Run lifecycle is a time-ordered sequence, not a per-run sequence counter,
    # so sort by occurred_at then a stable event-type priority (plan -> ... ->
    # terminal). This differs from the cross-run order_key used for replay.
    priority = {"plan": 0, "file": 1, "command": 2, "test": 3, "artifact": 4,
                "approval": 5, "warning": 6, "terminal": 7}
    envelopes.sort(key=lambda e: (e.occurred_at, priority.get(e.event_type, 99)))
    return [to_activity_item(e) for e in envelopes]


# --- Audit JSON/CSV export (acceptance item 5) --------------------------------
#
# Exported rows must match the in-app activity contract (the same EventEnvelope
# / ActivityItem the UI renders) so a downloaded audit and the live activity
# stream cannot silently diverge. Both serializers are pure DTO (no DB /
# websocket imports) and deterministic (sort-stable + canonical_json), so an
# export is byte-reproducible for the same inputs.


def export_events_json(events: list[EventEnvelope]) -> str:
    ordered = sorted(events, key=lambda e: (e.occurred_at, e.event_type, e.event_id))
    return json.dumps(
        {
            "schema_version": CURRENT_SCHEMA_VERSION,
            "count": len(ordered),
            "events": [json.loads(e.canonical_json()) for e in ordered],
        },
        sort_keys=True,
        separators=(",", ":"),
    )


def export_events_csv(events: list[EventEnvelope]) -> str:
    ordered = sorted(events, key=lambda e: (e.occurred_at, e.event_type, e.event_id))
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(
        ["event_id", "event_type", "occurred_at", "actor_id", "channel_id",
         "run_id", "correlation_id", "sequence", "kind", "summary"]
    )
    for env in ordered:
        item = to_activity_item(env)
        writer.writerow([
            item.event_id,
            item.event_type,
            env.occurred_at.isoformat(),
            item.actor_id or "",
            env.channel_id or "",
            item.run_id or "",
            item.correlation_id or "",
            "" if item.sequence is None else item.sequence,
            item.kind,
            item.summary,
        ])
    return buffer.getvalue()
