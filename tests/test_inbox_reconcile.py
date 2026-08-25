"""M6.8 slice 2 — Items dedupe deterministically and update/resolve with their source
record (acceptance item 2).

Because item ids are derived from the source record, reprojecting the same source
record MUST update the existing item in place (not create a second one), a source
that clears its attention state MUST drop/resolve the item, and inbox-local state
(acknowledged/owner) MUST survive re-projection. reconcile_inbox_for_team is the
idempotent reconciler that maintains a previous projection against a fresh source
scan.
"""
from __future__ import annotations

from dataclasses import replace

from crewspace.application.inbox import InboxItem, project_inbox_for_team, reconcile_inbox_for_team


def _rec(source_type, source_id, status, team_id, **kw):
    return {"source_type": source_type, "source_id": source_id, "status": status,
            "team_id": team_id, **kw}


def test_reprojection_is_idempotent_and_updates_in_place():
    records = [_rec("coding_run", "run_1", "failed", "team_a", summary="boom")]
    first = project_inbox_for_team(records, "team_a")
    # same source, new summary -> reconcile must keep ONE item with the new summary
    records2 = [_rec("coding_run", "run_1", "failed", "team_a", summary="boom v2")]
    second = reconcile_inbox_for_team(first, records2, "team_a")
    assert len(second) == 1
    assert second[0].source_id == "run_1"
    assert second[0].summary == "boom v2"  # updated in place, not duplicated


def test_resolved_source_drops_the_item():
    records = [_rec("coding_run", "run_1", "failed", "team_a")]
    first = project_inbox_for_team(records, "team_a")
    assert len(first) == 1
    # source record now succeeded -> no longer an attention state -> item gone
    records2 = [_rec("coding_run", "run_1", "succeeded", "team_a")]
    second = reconcile_inbox_for_team(first, records2, "team_a")
    assert second == []  # item removed with its source, no orphan


def test_inbox_local_state_survives_reprojection():
    records = [_rec("coding_run", "run_1", "failed", "team_a")]
    first = project_inbox_for_team(records, "team_a")
    first[0] = replace(first[0], acknowledged=True, owner_id="u_bilal")
    # re-scan: source still failed, so the item persists with its ack/owner intact
    records2 = [_rec("coding_run", "run_1", "failed", "team_a", summary="boom v2")]
    second = reconcile_inbox_for_team(first, records2, "team_a")
    assert len(second) == 1
    assert second[0].acknowledged is True
    assert second[0].owner_id == "u_bilal"
    assert second[0].summary == "boom v2"  # source-derived fields refresh; local state kept


def test_team_scoping_holds_during_reconcile():
    records = [_rec("coding_run", "run_a", "failed", "team_a"),
               _rec("coding_run", "run_b", "failed", "team_b")]
    first = project_inbox_for_team(records, "team_a")  # only team_a item
    assert len(first) == 1
    # a team_b record never enters team_a's inbox, even on reconcile
    records2 = [_rec("coding_run", "run_b", "timed_out", "team_b")]
    second = reconcile_inbox_for_team(first, records2, "team_a")
    assert all(i.team_id == "team_a" for i in second)
    assert "run_b" not in {i.source_id for i in second}
