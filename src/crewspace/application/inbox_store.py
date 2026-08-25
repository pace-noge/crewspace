"""M6.8 app-shell inbox state seam.

The store retains only inbox-local presentation state (owner, acknowledgement,
resolution). Source-derived fields are refreshed through reconcile_inbox_for_team;
source records remain the source of truth.
"""
from __future__ import annotations

from crewspace.application.inbox import (
    InboxFilters,
    InboxItem,
    InboxView,
    acknowledge_item,
    assign_item,
    build_inbox_view,
    reconcile_inbox_for_team,
    resolve_item,
)
from crewspace.application.inbox_events import inbox_events


class InboxStore:
    """Team-keyed in-memory store for inbox-local state only."""

    def __init__(self) -> None:
        self._items: dict[str, list[InboxItem]] = {}

    def reset(self) -> None:
        self._items.clear()

    def reconcile(self, team_id: str, records: list[dict]) -> list[InboxItem]:
        items = reconcile_inbox_for_team(self._items.get(team_id, []), records, team_id)
        self._items[team_id] = items
        inbox_events.publish(team_id, items)
        return list(items)

    def view(self, team_id: str, filters: InboxFilters | None = None) -> InboxView:
        return build_inbox_view(self._items.get(team_id, []), filters)

    def acknowledge(self, team_id: str, item_id: str) -> bool:
        return self._update(team_id, item_id, acknowledge_item)

    def assign(self, team_id: str, item_id: str, owner_id: str) -> bool:
        return self._update(team_id, item_id, assign_item, owner_id)

    def resolve(self, team_id: str, item_id: str) -> bool:
        return self._update(team_id, item_id, resolve_item)

    def _update(self, team_id: str, item_id: str, operation, *args) -> bool:
        current = self._items.get(team_id, [])
        if not any(item.item_id == item_id for item in current):
            return False
        self._items[team_id] = operation(current, item_id, *args)
        inbox_events.publish(team_id, self._items[team_id])
        return True


inbox_store = InboxStore()
