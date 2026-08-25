"""M6.8 slice 1 — Inbox item taxonomy + source-to-item projection rules.

The operational inbox is a PROJECTION over source records/events (coding runs,
change sets, workflow runs, agents, MCP tools), never a second source of truth.
This module defines the item taxonomy, the documented source-to-item mapping
(INBOX_RULES), and a deterministic, team-scoped projection so items dedupe by a
source-derived id and never leak across tenants. Acceptance item 1.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import List, Optional

# Deterministic deep-link patterns per source type (real existing routes where they
# exist; the inbox only renders links, it does not own the records).
_DEEP_LINK = {
    "coding_run": "/api/coding/runs/{source_id}",
    "change_set": "/management/change-sets/{source_id}",
    "workflow_run": "/workflows/{source_id}",
    "agent": "/management/agents",
    "mcp_tool": "/management/mcp",
}


@dataclass(frozen=True)
class InboxItem:
    item_id: str          # derived from source (deterministic -> dedup by construction)
    kind: str
    source_type: str
    source_id: str
    team_id: str
    priority: int        # higher = more urgent
    summary: str
    owner_id: Optional[str] = None
    created_at: str = ""
    acknowledged: bool = False   # inbox-local state (survives re-projection)
    resolved: bool = False
    deep_link: str = ""

    @property
    def is_actionable(self) -> bool:
        return not self.resolved


# Documented taxonomy: kind -> (priority, the source statuses that produce it).
# This is executable documentation of the source-to-item projection rules.
INBOX_RULES: dict = {
    "approval_request": {"priority": 90, "source_statuses": ("approval_required",)},
    "run_failed": {"priority": 80, "source_statuses": ("failed",)},
    "run_timed_out": {"priority": 85, "source_statuses": ("timed_out",)},
    "agent_disconnected": {"priority": 75, "source_statuses": ("disconnected",)},
    "workflow_failed": {"priority": 70, "source_statuses": ("failed",)},
    "mcp_approval_pending": {"priority": 60, "source_statuses": ("pending",)},
    "review_requested": {"priority": 50, "source_statuses": ("captured",)},
    "stale_task": {"priority": 30, "source_statuses": ("stale",)},
}

_KIND_BY_SOURCE: dict = {}  # (source_type, status) -> kind, built below
for _kind, _rule in INBOX_RULES.items():
    for _st in _rule["source_statuses"]:
        _KIND_BY_SOURCE[(_kind.split("_", 1)[0] if _kind in ("run_failed", "run_timed_out") else _kind, _st)] = _kind
# run_* kinds key off source_type "coding_run"
_KIND_BY_SOURCE[("coding_run", "failed")] = "run_failed"
_KIND_BY_SOURCE[("coding_run", "timed_out")] = "run_timed_out"
_KIND_BY_SOURCE[("change_set", "captured")] = "review_requested"
_KIND_BY_SOURCE[("workflow_run", "failed")] = "workflow_failed"
_KIND_BY_SOURCE[("agent", "disconnected")] = "agent_disconnected"
_KIND_BY_SOURCE[("mcp_tool", "pending")] = "mcp_approval_pending"
_KIND_BY_SOURCE[("coding_run", "approval_required")] = "approval_request"
_KIND_BY_SOURCE[("task", "stale")] = "stale_task"


def derive_inbox_id(source_type: str, source_id: str) -> str:
    """Deterministic id from the source record — dedup by construction, and the item
    updates/resolves with its source record (no independent lifecycle)."""
    return f"{source_type}:{source_id}"


def build_inbox_item(
    source_type: str,
    source_id: str,
    status: str,
    team_id: str,
    *,
    owner_id: Optional[str] = None,
    summary: str = "",
    created_at: str = "",
    resolved: bool = False,
) -> Optional["InboxItem"]:
    """Map a single source record to an InboxItem per INBOX_RULES.

    Returns None when the (source_type, status) is not an inbox-producing state
    (e.g. a succeeded run, a reviewed change set) — the inbox is a projection of
    things needing attention, not a mirror of every record.
    """
    kind = _KIND_BY_SOURCE.get((source_type, status))
    if kind is None:
        return None
    rule = INBOX_RULES[kind]
    link = _DEEP_LINK.get(source_type, "").format(source_id=source_id)
    return InboxItem(
        item_id=derive_inbox_id(source_type, source_id),
        kind=kind,
        source_type=source_type,
        source_id=source_id,
        team_id=team_id,
        priority=rule["priority"],
        summary=summary or f"{kind} ({source_type} {source_id})",
        owner_id=owner_id,
        created_at=created_at,
        resolved=resolved,
        deep_link=link,
    )


def project_inbox_for_team(records: List[dict], team_id: str) -> List[InboxItem]:
    """Team-scoped projection over source records.

    Fail-closed on tenancy: any record whose team_id != the requested team_id is
    DROPPED (never projected), so the inbox cannot leak cross-tenant information.
    Items are returned deduped by their deterministic source-derived id and sorted
    by descending priority (most urgent first).
    """
    items: dict = {}
    for rec in records:
        rec_team = rec.get("team_id")
        if rec_team != team_id:
            continue  # cross-tenant record excluded
        item = build_inbox_item(
            source_type=rec["source_type"],
            source_id=rec["source_id"],
            status=rec["status"],
            team_id=team_id,
            owner_id=rec.get("owner_id"),
            summary=rec.get("summary", ""),
            created_at=rec.get("created_at", ""),
            resolved=bool(rec.get("resolved", False)),
        )
        if item is None:
            continue
        items[item.item_id] = item  # dedup by deterministic id
    return sorted(items.values(), key=lambda i: (-i.priority, i.item_id))


def reconcile_inbox_for_team(
    previous: List[InboxItem], records: List[dict], team_id: str
) -> List[InboxItem]:
    """Idempotent reconcile of a prior projection against a fresh source scan.

    Because item ids are derived from the source record, this keeps the inbox a pure
    projection:
      - A source that is still in an attention state updates the SAME item in place
        (source-derived fields such as summary/status refresh); inbox-local state
        (acknowledged, owner_id) is preserved from the previous item.
      - A source that has cleared its attention state (no longer maps to a kind) drops
        its item — the item resolves with its source record, never orphaned.
      - A brand-new attention state produces a fresh item.
      - Cross-tenant records are still excluded (fail-closed on tenancy).
    """
    from dataclasses import replace

    projected = project_inbox_for_team(records, team_id)
    prev_by_id = {i.item_id: i for i in previous}
    out: dict = {}
    for item in projected:
        prior = prev_by_id.get(item.item_id)
        if prior is not None:
            # refresh source-derived fields, keep inbox-local state
            item = replace(
                item,
                acknowledged=prior.acknowledged,
                owner_id=prior.owner_id if prior.owner_id is not None else item.owner_id,
            )
        out[item.item_id] = item
    return sorted(out.values(), key=lambda i: (-i.priority, i.item_id))


def load_inbox_for_team(
    records: List[dict], team_id: str, *, principal_team_id: Optional[str]
) -> List[InboxItem]:
    """Authorization gate over the team-scoped projection (acceptance item 3).

    A team_id ARGUMENT is not an authorization decision. This wrapper re-checks the
    principal's team membership FIRST and FAILS CLOSED: a principal who is not a
    member of `team_id` receives an empty list regardless of what records or team_id
    are passed, so cross-tenant information can never leak through the inbox.
    """
    if not principal_team_id or principal_team_id != team_id:
        return []
    return project_inbox_for_team(records, team_id)
