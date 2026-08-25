"""M6.8 slice 7 — integration POC across every supported source."""
from __future__ import annotations

from crewspace.application.inbox_poc import run_inbox_poc, seeded_inbox_records


EXPECTED_KINDS = (
    "agent_disconnected",
    "approval_request",
    "mcp_approval_pending",
    "review_requested",
    "run_failed",
    "run_timed_out",
    "stale_task",
    "workflow_failed",
)


def test_seeded_poc_exercises_every_supported_kind_and_source():
    report = run_inbox_poc()
    assert len(report.item_ids) == 8
    assert report.kinds == EXPECTED_KINDS
    assert len(seeded_inbox_records()) == 8
    assert all(link.startswith("/") and "{" not in link for link in report.deep_links)


def test_poc_is_deterministic_authorized_and_replayable():
    first = run_inbox_poc()
    second = run_inbox_poc()
    assert first.item_ids == second.item_ids
    assert first.kinds == second.kinds and first.deep_links == second.deep_links
    assert first.cross_tenant_visible == 0
    assert first.unread_before == 8 and first.unread_after == 7
    assert [event.event_type for event in first.replay] == ["upsert", "upsert", "unread_count", "upsert"]
    sequences = [event.sequence for event in first.replay]
    assert sequences == sorted(sequences) and len(sequences) == len(set(sequences))
    assert first.replay[-1].unread_count == 7


def test_poc_covers_each_source_family():
    source_types = {record["source_type"] for record in seeded_inbox_records()}
    assert source_types == {"coding_run", "change_set", "workflow_run", "agent", "mcp_tool", "task"}
