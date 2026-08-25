"""M6.8 final integration POC over every operational-inbox source/kind."""
from __future__ import annotations

from dataclasses import dataclass

from crewspace.application.inbox import load_inbox_for_team
from crewspace.application.inbox_events import InboxEvent, InboxEventStream, count_unread
from crewspace.application.inbox_store import InboxStore


@dataclass(frozen=True)
class InboxPocReport:
    item_ids: tuple[str, ...]
    kinds: tuple[str, ...]
    deep_links: tuple[str, ...]
    unread_before: int
    unread_after: int
    replay: tuple[InboxEvent, ...]
    cross_tenant_visible: int


def seeded_inbox_records(team_id: str = "team_poc") -> list[dict]:
    """One record for each of the 8 supported inbox item kinds."""
    def record(source_type, source_id, status, summary, deep_link_id=None):
        row = {
            "source_type": source_type,
            "source_id": source_id,
            "status": status,
            "team_id": team_id,
            "summary": summary,
            "created_at": "2026-08-25T00:00:00Z",
        }
        if deep_link_id is not None:
            row["deep_link_id"] = deep_link_id
        return row

    return [
        record("coding_run", "run_approval", "approval_required", "Approve delivery"),
        record("coding_run", "run_failed", "failed", "Coding run failed"),
        record("coding_run", "run_timeout", "timed_out", "Coding run timed out"),
        record("agent", "agent_remote", "disconnected", "Agent disconnected"),
        record("workflow_run", "workflow_run_1", "failed", "Workflow failed", "workflow_1"),
        record("mcp_tool", "jira.create", "pending", "MCP approval pending", "mcp_jira"),
        record("change_set", "change_1", "captured", "Review requested"),
        record("task", "card_stale", "stale", "Task is stale", "board_main"),
    ]


def run_inbox_poc() -> InboxPocReport:
    team_id = "team_poc"
    records = seeded_inbox_records(team_id)
    authorized = load_inbox_for_team(records, team_id, principal_team_id=team_id)
    denied = load_inbox_for_team(records, team_id, principal_team_id="team_other")

    events = InboxEventStream()
    store = InboxStore(event_stream=events)
    items = store.reconcile(team_id, records)
    unread_before = count_unread(items)
    cursor = events.cursor(team_id)

    first = items[0]
    store.assign(team_id, first.item_id, "u_operator")
    store.acknowledge(team_id, first.item_id)
    store.resolve(team_id, first.item_id)
    current = store.view(team_id).items
    replay = events.events_after(team_id, cursor)

    assert {item.item_id for item in authorized} == {item.item_id for item in items}
    return InboxPocReport(
        item_ids=tuple(item.item_id for item in items),
        kinds=tuple(sorted(item.kind for item in items)),
        deep_links=tuple(item.deep_link for item in items),
        unread_before=unread_before,
        unread_after=count_unread(current),
        replay=tuple(replay),
        cross_tenant_visible=len(denied),
    )
