"""M6.8 slice 3 — Authorization prevents cross-tenant information leakage (item 3).

The projection is team-scoped, but a team_id ARGUMENT is not an authorization
decision. load_inbox_for_team wraps project_inbox_for_team and re-checks the
principal's team membership FIRST: a principal who is not a member of the requested
team receives an empty list no matter what records or team_id are passed. Fail-closed.
"""
from __future__ import annotations

from crewspace.application.inbox import load_inbox_for_team, project_inbox_for_team


def _rec(source_type, source_id, status, team_id, **kw):
    return {"source_type": source_type, "source_id": source_id, "status": status,
            "team_id": team_id, **kw}


def test_member_can_load_their_own_team_inbox():
    records = [_rec("coding_run", "run_a", "failed", "team_a")]
    items = load_inbox_for_team(records, "team_a", principal_team_id="team_a")
    assert len(items) == 1 and items[0].source_id == "run_a"


def test_other_team_principal_gets_nothing_fail_closed():
    # principal is team_b, but a caller passes team_a's records + team_id.
    records = [_rec("coding_run", "run_a", "failed", "team_a"),
               _rec("change_set", "cs_a", "captured", "team_a")]
    items = load_inbox_for_team(records, "team_a", principal_team_id="team_b")
    assert items == []  # no cross-tenant leakage, even with valid team_a data in scope


def test_unauthenticated_principal_is_denied():
    records = [_rec("coding_run", "run_a", "failed", "team_a")]
    assert load_inbox_for_team(records, "team_a", principal_team_id=None) == []
    assert load_inbox_for_team(records, "team_a", principal_team_id="") == []


def test_membership_check_is_enforced_before_projection():
    # projection alone would yield items; the auth gate must short-circuit to [].
    records = [_rec("coding_run", "run_a", "failed", "team_a")]
    projected = project_inbox_for_team(records, "team_a")
    assert len(projected) == 1  # proves the gate adds protection, not the projection
    assert load_inbox_for_team(records, "team_a", principal_team_id="team_x") == []
