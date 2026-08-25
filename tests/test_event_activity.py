"""M6.4 slice 4 — Compact typed activity rendering (acceptance item 4).

Acceptance item 4: "UI renders compact typed activity with raw logs available
on demand." The contract side of this slice is the DTO mapper + compact-summary
helper (pure, testable without a browser) and the run-detail endpoint carrying a
derived `activity` list + the raw `recent_output` as the on-demand "raw logs".
The HTML fragment is the UI surface; it is rendered server-side so the compact
rows and the raw-logs toggle are inspectable without a live socket.
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest
from starlette.testclient import TestClient

from crewspace.api.rendering import templates
from crewspace.dto.events import (
    ActivityItem,
    EventEnvelope,
    build_event,
    compact_summary,
    run_to_activity,
    to_activity_item,
)


def _now() -> datetime:
    return datetime(2026, 8, 25, 12, 0, 0, tzinfo=timezone.utc)


def test_to_activity_item_round_trips_envelope_fields():
    env = build_event(
        "command",
        occurred_at=_now(),
        actor_id="agent_planner",
        run_id="run_1",
        sequence=0,
        payload={"command": "pytest", "exit_code": 0},
    )
    item = to_activity_item(env)
    assert isinstance(item, ActivityItem)
    assert item.event_id == env.event_id
    assert item.event_type == "command"
    assert item.occurred_at == env.occurred_at
    assert item.actor_id == "agent_planner"
    assert item.run_id == "run_1"
    assert item.sequence == 0
    assert item.summary == "ran pytest (exit 0)"
    assert item.kind == "command"
    assert item.raw is not None  # raw payload is always retained for on-demand view


def test_compact_summary_covers_each_event_type():
    cases = {
        "plan": ({"summary": "Implement login"}, "plan: Implement login"),
        "file": ({"path": "src/a.py", "action": "write"}, "file src/a.py: write"),
        "command": ({"command": "ls", "exit_code": 2}, "ran ls (exit 2)"),
        "test": ({"status": "passed", "passed": 3, "failed": 0}, "tests passed 3/3"),
        "artifact": ({"path": "dist/x.zip", "size_bytes": 10, "kind": "bundle"}, "artifact dist/x.zip (10 B)"),
        "approval": ({"decision": "granted", "action_class": "git_push"}, "approval granted: git_push"),
        "warning": ({"code": "W1", "message": "slow", "severity": "warning"}, "warning W1: slow"),
        "terminal": ({"status": "succeeded"}, "terminal: succeeded"),
    }
    for et, (payload, expected) in cases.items():
        env = build_event(et, occurred_at=_now(), payload=payload)
        assert compact_summary(env) == expected, et


def test_compact_summary_is_bounded_for_long_payloads():
    env = build_event("plan", occurred_at=_now(), payload={"summary": "x" * 5000})
    s = compact_summary(env)
    assert len(s) <= 200  # compact row must stay short


def test_activity_item_extra_fields_forbidden():
    # ActivityItem is FrozenDTO-like (frozen + extra forbid) so the UI contract
    # cannot drift with ad-hoc keys.
    with pytest.raises(Exception):
        ActivityItem(  # type: ignore[call-arg]
            event_id="e",
            event_type="terminal",
            occurred_at=_now(),
            kind="terminal",
            summary="x",
            raw={},
            surprise="nope",
        )


# --- run_to_activity: derive compact activity from a real CodingRun ----------


def _fake_run(**kw):
    class _R:
        pass
    r = _R()
    for k, v in kw.items():
        setattr(r, k, v)
    return r


def test_run_to_activity_derives_typed_events_in_order():
    run = _fake_run(
        id="run_9",
        agent_id="agent_planner",
        instruction="Refactor auth",
        status="succeeded",
        created_at=_now(),
        started_at=_now(),
        finished_at=_now(),
        failure_reason="",
        recent_output="",
    )
    items = run_to_activity(run)
    kinds = [i.event_type for i in items]
    assert kinds == ["plan", "command", "terminal"]
    summaries = " ".join(i.summary for i in items)
    assert "Refactor auth" in summaries
    assert "terminal: succeeded" in summaries
    # each item keeps its raw payload for on-demand view
    assert all(i.raw for i in items)


# --- Endpoint: run detail carries activity + raw-logs flag -------------------


def test_run_detail_carries_activity_and_raw_logs_flag(client, app):
    from crewspace.api.connection import agent_manager
    from crewspace.application.coding_runs import dispatch_coding_run
    from crewspace.application.change_sets import ChangeSetService
    from crewspace.dto.change_sets import ChangeSetDTO, ChangeCommitDTO, ChangedFileDTO
    import asyncio
    from crewspace.domain.entities import TeamRepositoryAccess
    import datetime as dt

    async def arrange() -> tuple[str, str]:
        async with app.state.db.uow() as uow:
            await uow.coding_repositories.grant_team(
                TeamRepositoryAccess(
                    team_id="team_acme", repository_id="repo_act",
                    granted_by="user_bilal", granted_at=dt.datetime.now(dt.timezone.utc),
                )
            )
            await uow.commit()
            run = await dispatch_coding_run(
                uow, agent_id="agent_planner", team_id="team_acme",
                repository_id="repo_act", run_id="run_act",
                instruction="ship it", requested_by="user_bilal",
                agent_manager=_FakeManager(),
            )
        return run.id, run.request_id

    original = agent_manager.send_coding_run
    try:
        agent_manager.send_coding_run = lambda *a, **k: None
        run_id, request_id = asyncio.run(arrange())

        cs = ChangeSetDTO(
            repository_id="repo_act", run_id=run_id,
            branch="main", base_commit="a" * 40, head_commit="b" * 40,
            commits=(ChangeCommitDTO(sha="a" * 40, subject="x"),),
            files=(ChangedFileDTO(path="f.py", status="added", additions=1, deletions=0),),
            additions=1, deletions=0,
            verification=(), artifacts=(),
        )

        async def capture():
            async with app.state.db.uow() as uow:
                await ChangeSetService().record_capture(
                    agent_id="agent_planner", request_id=request_id,
                    change_set=cs, uow=uow,
                )
                await uow.commit()

        asyncio.run(capture())

        client.post("/auth/login", data={"username": "Bilal", "password": "admin123"})
        resp = client.get(f"/api/coding/runs/{run_id}", headers={"Origin": "http://testserver"})
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert "activity" in body and isinstance(body["activity"], list)
        assert body["activity"][0]["event_type"] == "plan"
        assert "ship it" in body["activity"][0]["summary"]
        assert body["has_raw_logs"] is False  # no recent_output yet
        assert "timeline" in body  # existing contract preserved
    finally:
        agent_manager.send_coding_run = original


class _FakeManager:
    async def send_coding_run(self, *a, **k):
        return None


# --- Fragment renders compact rows + raw toggle + raw-logs section -----------


def test_activity_list_fragment_renders_compact_rows_and_raw_toggle():
    env = build_event(
        "command", occurred_at=_now(), run_id="run_1",
        payload={"command": "pytest", "exit_code": 0},
    )
    items = [to_activity_item(env)]
    html = templates.get_template("activity_list.html").render(
        activity=[i.model_dump(mode="json") for i in items],
        has_raw_logs=False, recent_output="",
    )
    assert "activity-row" in html
    assert "ran pytest (exit 0)" in html
    assert "activity-raw-toggle" in html  # raw available on demand
    assert "activity-raw-logs" not in html  # only when has_raw_logs


def test_activity_list_fragment_renders_raw_logs_section_when_present():
    html = templates.get_template("activity_list.html").render(
        activity=[], has_raw_logs=True, recent_output="line one\nline two",
    )
    assert "activity-raw-logs" in html
    assert "line one" in html and "line two" in html

