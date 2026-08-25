"""M6.8 slice 1 — Inbox taxonomy + source-to-item projection rules (acceptance item 1).

The inbox is a PROJECTION over source records, not a second source of truth: item
ids are derived deterministically from the source (so dedup is by construction and
items resolve with their source record), and projection is team-scoped (no
cross-tenant leakage).
"""
from __future__ import annotations

from crewspace.application.inbox import (
    INBOX_RULES,
    InboxItem,
    build_inbox_item,
    derive_inbox_id,
    project_inbox_for_team,
)


def _rec(source_type, source_id, status, team_id, **kw):
    return {"source_type": source_type, "source_id": source_id, "status": status,
            "team_id": team_id, **kw}


def test_taxonomy_documents_all_expected_kinds_with_priority():
    for kind in ("approval_request", "run_failed", "run_timed_out", "agent_disconnected",
                 "workflow_failed", "mcp_approval_pending", "review_requested", "stale_task"):
        assert kind in INBOX_RULES
        assert isinstance(INBOX_RULES[kind]["priority"], int)
    # higher-severity items rank above lower-severity ones
    assert INBOX_RULES["run_timed_out"]["priority"] > INBOX_RULES["review_requested"]["priority"]
    assert INBOX_RULES["review_requested"]["priority"] > INBOX_RULES["stale_task"]["priority"]


def test_inbox_id_is_deterministic_and_dedupes():
    a = derive_inbox_id("coding_run", "run_1")
    b = derive_inbox_id("coding_run", "run_1")
    c = derive_inbox_id("coding_run", "run_2")
    assert a == b and a != c  # same source -> same id; distinct sources -> distinct ids


def test_failed_run_projects_to_run_failed_item():
    item = build_inbox_item("coding_run", "run_1", "failed", "team_a", summary="boom")
    assert isinstance(item, InboxItem)
    assert item.kind == "run_failed"
    assert item.priority == INBOX_RULES["run_failed"]["priority"]
    assert item.deep_link == "/api/coding/runs/run_1"
    assert item.resolved is False


def test_non_attention_state_is_not_an_inbox_item():
    # a succeeded run / reviewed change set are not inbox-producing states
    assert build_inbox_item("coding_run", "run_x", "succeeded", "team_a") is None
    assert build_inbox_item("change_set", "cs_x", "reviewed", "team_a") is None


def test_projection_is_team_scoped_no_cross_tenant_leakage():
    records = [
        _rec("coding_run", "run_a", "failed", "team_a"),
        _rec("coding_run", "run_b", "timed_out", "team_a"),
        _rec("change_set", "cs_x", "captured", "team_a"),  # review_requested
        _rec("coding_run", "run_other", "failed", "team_b"),  # other tenant
    ]
    items = project_inbox_for_team(records, "team_a")
    ids = {i.source_id for i in items}
    assert "run_a" in ids and "run_b" in ids and "cs_x" in ids
    # the team_b record is excluded entirely -> no cross-tenant leakage
    assert "run_other" not in ids
    # dedupe: two projections of the same source collapse to one
    dup = project_inbox_for_team(records + [_rec("coding_run", "run_a", "failed", "team_a")], "team_a")
    assert sum(1 for i in dup if i.source_id == "run_a") == 1
    # sorted by descending priority (timed_out > failed > review_requested)
    assert [i.kind for i in items] == ["run_timed_out", "run_failed", "review_requested"]
