"""M6.4 slice 5 — Audit JSON/CSV export of canonical events (acceptance item 5).

Acceptance item 5: "Audit JSON/CSV exports include the same canonical events."
The exported rows must match the in-app activity contract (the same
`EventEnvelope`/`ActivityItem` we render in the UI), so a downloaded audit and
the live activity stream cannot silently diverge. The serializers are pure DTO
(no DB/websocket imports) and deterministic (sort-stable), so an export is
byte-reproducible for the same inputs.
"""
from __future__ import annotations

import csv
import io
import json

from crewspace.dto.events import (
    EventEnvelope,
    build_event,
    export_events_csv,
    export_events_json,
    to_activity_item,
)


def _now():
    from datetime import datetime, timezone
    return datetime(2026, 8, 25, 12, 0, 0, tzinfo=timezone.utc)


def _sample_events():
    return [
        build_event("plan", occurred_at=_now(), run_id="run_1", actor_id="agent_planner",
                    payload={"summary": "Ship login"}),
        build_event("command", occurred_at=_now(), run_id="run_1", actor_id="agent_planner",
                    payload={"command": "pytest", "exit_code": 0}),
        build_event("terminal", occurred_at=_now(), run_id="run_1", actor_id="agent_planner",
                    payload={"status": "succeeded"}),
    ]


def test_export_events_json_uses_canonical_json_per_event():
    events = _sample_events()
    doc = export_events_json(events)
    parsed = json.loads(doc)
    assert parsed["schema_version"] == "1.0"
    assert parsed["count"] == 3
    # each row is a canonical envelope (same shape as canonical_json)
    types = {e["event_type"] for e in parsed["events"]}
    assert types == {"plan", "command", "terminal"}
    # identical occurred_at -> sorted by (event_type, event_id); command < plan < terminal
    assert parsed["events"][0]["event_type"] == "command"
    assert parsed["events"][-1]["event_type"] == "terminal"
    # round-trips through canonical_json for every event
    for env in events:
        assert json.loads(env.canonical_json()) in parsed["events"]


def test_export_events_json_is_deterministic():
    events = _sample_events()
    assert export_events_json(events) == export_events_json(list(events))


def test_export_events_csv_has_header_and_one_row_per_event():
    events = _sample_events()
    doc = export_events_csv(events)
    reader = list(csv.DictReader(io.StringIO(doc)))
    assert len(reader) == 3
    assert {r["event_type"] for r in reader} == {"plan", "command", "terminal"}
    assert reader[0]["event_type"] == "command"  # sort order
    assert reader[-1]["event_type"] == "terminal"
    # canonical event columns present
    for row in reader:
        assert set(["event_id", "event_type", "occurred_at", "run_id", "kind", "summary"]).issubset(row.keys())


def test_export_csv_rows_match_in_app_activity_contract():
    # The exported summary + kind must equal what the UI activity renders
    # (to_activity_item), so audit and live activity cannot diverge.
    events = _sample_events()
    doc = export_events_csv(events)
    rows = list(csv.DictReader(io.StringIO(doc)))
    by_type = {row["event_type"]: row for row in rows}
    for env in events:
        item = to_activity_item(env)
        row = by_type[item.event_type]
        assert row["summary"] == item.summary
        assert row["kind"] == item.kind
        assert row["event_id"] == item.event_id


def test_export_csv_is_deterministic_and_parseable():
    events = _sample_events()
    assert export_events_csv(events) == export_events_csv(list(events))


# --- Endpoint: run events export reachable + authorized ----------------------


def test_export_coding_run_events_json_and_csv(client, app):
    from crewspace.api.connection import agent_manager
    from crewspace.application.coding_runs import dispatch_coding_run
    from crewspace.application.change_sets import ChangeSetService
    from crewspace.dto.change_sets import (
        ChangeSetDTO, ChangeCommitDTO, ChangedFileDTO,
    )
    import asyncio
    from crewspace.domain.entities import TeamRepositoryAccess
    import datetime as dt

    original = agent_manager.send_coding_run
    try:
        agent_manager.send_coding_run = lambda *a, **k: None

        async def arrange():
            async with app.state.db.uow() as uow:
                await uow.coding_repositories.grant_team(
                    TeamRepositoryAccess(
                        team_id="team_acme", repository_id="repo_exp",
                        granted_by="user_bilal",
                        granted_at=dt.datetime.now(dt.timezone.utc),
                    )
                )
                await uow.commit()
                run = await dispatch_coding_run(
                    uow, agent_id="agent_planner", team_id="team_acme",
                    repository_id="repo_exp", run_id="run_exp",
                    instruction="export me", requested_by="user_bilal",
                    agent_manager=_FakeManager(),
                )
            return run.id, run.request_id

        run_id, request_id = asyncio.run(arrange())
        cs = ChangeSetDTO(
            repository_id="repo_exp", run_id=run_id, branch="main",
            base_commit="a" * 40, head_commit="b" * 40,
            commits=(ChangeCommitDTO(sha="a" * 40, subject="x"),),
            files=(ChangedFileDTO(path="f.py", status="added", additions=1, deletions=0),),
            additions=1, deletions=0, verification=(), artifacts=(),
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

        # JSON default
        rj = client.get(f"/api/coding/runs/{run_id}/events/export",
                        headers={"Origin": "http://testserver"})
        assert rj.status_code == 200, rj.text
        body = rj.json()
        assert body["schema_version"] == "1.0"
        assert body["count"] == 3
        assert {e["event_type"] for e in body["events"]} == {"plan", "command", "terminal"}

        # CSV
        rc = client.get(f"/api/coding/runs/{run_id}/events/export?format=csv",
                        headers={"Origin": "http://testserver"})
        assert rc.status_code == 200
        assert rc.headers["content-type"].startswith("text/csv")
        rows = list(csv.DictReader(io.StringIO(rc.text)))
        assert len(rows) == 3
        assert {r["event_type"] for r in rows} == {"plan", "command", "terminal"}
        # CSV summary matches the in-app activity contract for the same run
        detail = client.get(f"/api/coding/runs/{run_id}",
                            headers={"Origin": "http://testserver"}).json()
        summaries = {a["event_type"]: a["summary"] for a in detail["activity"]}
        for row in rows:
            assert row["summary"] == summaries[row["event_type"]]

        # Authorization: unknown run -> 404
        missing = client.get("/api/coding/runs/nope/events/export",
                             headers={"Origin": "http://testserver"})
        assert missing.status_code == 404
    finally:
        agent_manager.send_coding_run = original


class _FakeManager:
    async def send_coding_run(self, *a, **k):
        return None
