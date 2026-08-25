"""M6.8 live inbox updates and reconnect replay contract."""
from __future__ import annotations

from dataclasses import dataclass

from crewspace.application.inbox import InboxItem


@dataclass(frozen=True)
class InboxEvent:
    sequence: int
    team_id: str
    event_type: str  # upsert | remove | unread_count
    item_id: str | None
    unread_count: int
    item: InboxItem | None = None


def count_unread(items: list[InboxItem]) -> int:
    """Unread means both unacknowledged and unresolved."""
    return sum(1 for item in items if not item.acknowledged and not item.resolved)


class InboxEventStream:
    """Monotonic team-scoped stream supporting cursor replay without duplicates."""

    def __init__(self) -> None:
        self._events: dict[str, list[InboxEvent]] = {}
        self._sequence: dict[str, int] = {}
        self._snapshots: dict[str, dict[str, InboxItem]] = {}

    def reset(self) -> None:
        self._events.clear()
        self._sequence.clear()
        self._snapshots.clear()

    def publish(self, team_id: str, items: list[InboxItem]) -> list[InboxEvent]:
        previous = self._snapshots.get(team_id, {})
        current = {item.item_id: item for item in items}
        emitted: list[InboxEvent] = []
        unread = count_unread(items)

        for item_id in sorted(set(previous) - set(current)):
            emitted.append(self._append(team_id, "remove", item_id, unread))
        for item_id in sorted(current):
            item = current[item_id]
            if previous.get(item_id) != item:
                emitted.append(self._append(team_id, "upsert", item_id, unread, item))

        previous_unread = count_unread(list(previous.values()))
        if not self._events.get(team_id) or previous_unread != unread:
            emitted.append(self._append(team_id, "unread_count", None, unread))

        self._snapshots[team_id] = current
        return emitted

    def events_after(self, team_id: str, cursor: int | None) -> list[InboxEvent]:
        """Replay strictly after cursor; malformed/negative cursors fail closed at 0."""
        safe_cursor = cursor if isinstance(cursor, int) and cursor >= 0 else 0
        return [event for event in self._events.get(team_id, []) if event.sequence > safe_cursor]

    def cursor(self, team_id: str) -> int:
        return self._sequence.get(team_id, 0)

    def _append(
        self,
        team_id: str,
        event_type: str,
        item_id: str | None,
        unread_count: int,
        item: InboxItem | None = None,
    ) -> InboxEvent:
        sequence = self._sequence.get(team_id, 0) + 1
        self._sequence[team_id] = sequence
        event = InboxEvent(sequence, team_id, event_type, item_id, unread_count, item)
        self._events.setdefault(team_id, []).append(event)
        return event


inbox_events = InboxEventStream()
