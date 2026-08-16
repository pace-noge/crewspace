"""Scheduled channel instructions: UI, authorization, and execution."""
from __future__ import annotations

import asyncio

from crewspace.application.scheduling import SchedulerLoop


def test_superadmin_can_create_human_friendly_job(client):
    form = client.get("/cronjobs/new")
    assert form.status_code == 200
    assert "New scheduled instruction" in form.text
    assert "Channel" in form.text
    assert "Instruction" in form.text
    assert "Every" in form.text
    assert "Daily" in form.text
    assert "Once" in form.text
    assert "crontab" not in form.text.lower()

    response = client.post(
        "/cronjobs",
        data={
            "name": "Stand-up reminder",
            "description": "Remind the team every two hours",
            "channel_id": "chan_general",
            "instruction": "Daily stand-up reminder",
            "schedule_kind": "interval",
            "interval_value": "2",
            "interval_unit": "hours",
        },
        follow_redirects=True,
    )
    assert response.status_code == 200
    assert "Stand-up reminder" in response.text
    assert "Remind the team every two hours" in response.text
    assert "Daily stand-up reminder" in response.text
    assert "Every 2 hours" in response.text
    assert "Run now" in response.text


def test_run_now_posts_instruction_and_executes_mentioned_agent(client, app):
    created = client.post(
        "/cronjobs",
        data={
            "name": "Planner card creation",
            "channel_id": "chan_general",
            "instruction": '@planner new card "Scheduled card" in Todo',
            "schedule_kind": "interval",
            "interval_value": "1",
            "interval_unit": "hours",
        },
        follow_redirects=False,
    )
    assert created.status_code == 303
    job_id = created.headers["location"].rsplit("/", 1)[-1]

    run = client.post(f"/cronjobs/{job_id}/run", follow_redirects=True)
    assert run.status_code == 200
    assert "Last run succeeded" in run.text

    messages = client.get("/channels/chan_general/messages").json()
    assert any(m["body"] == '@planner new card "Scheduled card" in Todo' for m in messages)
    assert any("Created card" in m["body"] and "Scheduled card" in m["body"] for m in messages)

    async def card_exists():
        async with app.state.db.uow() as uow:
            return await uow.boards.find_card_by_title("board_main", "Scheduled card")

    assert asyncio.run(card_exists()) is not None


def test_team_member_can_create_and_run_own_channel_job(client, app):
    async def demote():
        async with app.state.db.uow() as uow:
            await uow._conn.execute(
                "UPDATE team_member SET role='member' WHERE team_id='team_acme' AND member_id='user_bilal'"
            )
            await uow._conn.execute(
                "UPDATE member SET role='team_member' WHERE id='user_bilal'"
            )

    asyncio.run(demote())
    home = client.get("/")
    assert 'href="/cronjobs"' in home.text
    assert client.get("/cronjobs").status_code == 200
    assert client.get("/cronjobs/new").status_code == 200
    created = client.post(
        "/cronjobs",
        data={
            "name": "My reminder",
            "description": "A personal team reminder",
            "channel_id": "chan_general",
            "instruction": "Team member reminder",
            "schedule_kind": "interval",
            "interval_value": "1",
            "interval_unit": "hours",
        },
        follow_redirects=False,
    )
    assert created.status_code == 303
    job_id = created.headers["location"].rsplit("/", 1)[-1]
    assert client.post(f"/cronjobs/{job_id}/run").status_code == 200


def test_team_member_cannot_access_another_creators_job(client, app):
    async def arrange():
        async with app.state.db.uow() as uow:
            await uow._conn.execute(
                "UPDATE team_member SET role='member' WHERE team_id='team_acme' AND member_id='user_bilal'"
            )
            await uow._conn.execute(
                "UPDATE member SET role='team_member' WHERE id='user_bilal'"
            )
            await uow._conn.execute(
                """INSERT INTO scheduled_job
                (id,name,description,channel_id,instruction,schedule_kind,interval_value,
                 interval_unit,creator_id,enabled,next_run_at,created_at)
                VALUES ('job_other','Other job',NULL,'chan_general','Not mine','interval',1,
                        'hours','agent_planner',1,'2027-01-01T00:00:00+00:00','2026-01-01T00:00:00+00:00')"""
            )

    asyncio.run(arrange())
    assert client.get("/cronjobs/job_other").status_code == 403
    assert client.get("/cronjobs/job_other/history").status_code == 403
    assert client.post("/cronjobs/job_other/run").status_code == 403
    listing = client.get("/cronjobs")
    assert listing.status_code == 200
    assert "Other job" not in listing.text


def test_create_cronjob_tool_is_registered(client):
    payload = client.get("/tools").json()
    tool = next(t for t in payload["tools"] if t["name"] == "create_cronjob")
    assert tool["input_schema"]["required"] == [
        "name", "channel_id", "instruction", "schedule_kind"
    ]
    assert "creator_id" not in tool["input_schema"]["properties"]


def test_each_manual_run_appends_a_detailed_execution_log(client):
    created = client.post(
        "/cronjobs",
        data={
            "name": "Execution logging",
            "channel_id": "chan_general",
            "instruction": "Execution log verification",
            "schedule_kind": "interval",
            "interval_value": "1",
            "interval_unit": "hours",
        },
        follow_redirects=False,
    )
    job_id = created.headers["location"].rsplit("/", 1)[-1]

    assert client.post(f"/cronjobs/{job_id}/run").status_code == 200
    assert client.post(f"/cronjobs/{job_id}/run").status_code == 200

    listing = client.get("/cronjobs")
    assert listing.status_code == 200
    assert "Run history" not in listing.text
    assert f'href="/cronjobs/{job_id}/history"' in listing.text
    assert 'id="schedule-search"' in listing.text
    assert 'id="schedule-status"' in listing.text

    detail = client.get(f"/cronjobs/{job_id}/history")
    assert detail.status_code == 200
    assert "Run history" in detail.text
    assert 'href="/cronjobs">Hide history</a>' in detail.text
    assert detail.text.count("Manual") == 2
    assert detail.text.count("Succeeded") >= 2
    assert "Execution log verification" in detail.text
    assert "Duration" in detail.text
    assert "Posted message" in detail.text
    assert "Next run" in detail.text
    assert '/runs/' in detail.text

    import re
    run_link = re.search(r'href="([^"]+/runs/[^"]+)"', detail.text)
    assert run_link is not None
    run_detail = client.get(run_link.group(1))
    assert run_detail.status_code == 200
    assert "Instruction snapshot" in run_detail.text
    assert "Execution log verification" in run_detail.text
    assert "Manual" in run_detail.text
    assert "Message IDs" in run_detail.text
    assert "Scheduled for" in run_detail.text
    assert "Started" in run_detail.text
    assert "Finished" in run_detail.text


def test_due_job_is_executed_by_scheduler(client, app):
    created = client.post(
        "/cronjobs",
        data={
            "name": "Automatic reminder",
            "channel_id": "chan_general",
            "instruction": "This came from the scheduler",
            "schedule_kind": "interval",
            "interval_value": "2",
            "interval_unit": "minutes",
        },
        follow_redirects=False,
    )
    job_id = created.headers["location"].rsplit("/", 1)[-1]

    async def run_due_job():
        async with app.state.db.uow() as uow:
            await uow._conn.execute(
                "UPDATE scheduled_job SET next_run_at = ? WHERE id = ?",
                ("2000-01-01T00:00:00+00:00", job_id),
            )
        scheduler = SchedulerLoop(app.state.db, app.state.settings)
        return await scheduler.run_due_once()

    assert asyncio.run(run_due_job()) == 1
    messages = client.get("/channels/chan_general/messages").json()
    assert any(message["body"] == "This came from the scheduler" for message in messages)


def test_two_scheduler_workers_claim_due_occurrence_once(client, app, monkeypatch):
    import datetime as dt

    from crewspace.application.scheduling import ScheduledJobService

    created = client.post(
        "/cronjobs",
        data={
            "name": "Claim once",
            "channel_id": "chan_general",
            "instruction": "Claimed scheduler work",
            "schedule_kind": "interval",
            "interval_value": "2",
            "interval_unit": "minutes",
        },
        follow_redirects=False,
    )
    job_id = created.headers["location"].rsplit("/", 1)[-1]
    calls: list[str] = []

    async def fake_run(self, job, uow, **kwargs):
        calls.append(job.id)
        await asyncio.sleep(0.05)
        await uow.scheduled_jobs.record_run(
            job.id,
            next_run_at=dt.datetime.now(dt.timezone.utc) + dt.timedelta(hours=1),
            enabled=True,
            status="succeeded",
            error=None,
            run_at=dt.datetime.now(dt.timezone.utc),
        )
        await uow.commit()
        return []

    monkeypatch.setattr(ScheduledJobService, "run", fake_run)

    async def race():
        async with app.state.db.uow() as uow:
            await uow._conn.execute(
                "UPDATE scheduled_job SET next_run_at=? WHERE id=?",
                ("2000-01-01T00:00:00+00:00", job_id),
            )
        first = SchedulerLoop(app.state.db, app.state.settings)
        second = SchedulerLoop(app.state.db, app.state.settings)
        return await asyncio.gather(first.run_due_once(), second.run_due_once())

    assert sum(asyncio.run(race())) == 1
    assert calls == [job_id]


def test_owner_can_edit_pause_resume_and_delete_job(client):
    created = client.post(
        "/cronjobs",
        data={
            "name": "Original reminder",
            "channel_id": "chan_general",
            "instruction": "Original instruction",
            "schedule_kind": "interval",
            "interval_value": "2",
            "interval_unit": "minutes",
        },
        follow_redirects=False,
    )
    job_id = created.headers["location"].rsplit("/", 1)[-1]

    edit_form = client.get(f"/cronjobs/{job_id}/edit")
    assert edit_form.status_code == 200
    assert "Edit scheduled instruction" in edit_form.text
    assert 'value="Original reminder"' in edit_form.text
    listing = client.get("/cronjobs")
    assert f'class="button" href="/cronjobs/{job_id}/edit"' in listing.text
    assert 'href="/cronjobs">Cancel' in edit_form.text

    updated = client.post(
        f"/cronjobs/{job_id}",
        data={
            "name": "Updated reminder",
            "description": "Changed details",
            "channel_id": "chan_general",
            "instruction": "Updated instruction",
            "schedule_kind": "interval",
            "interval_value": "5",
            "interval_unit": "minutes",
        },
        follow_redirects=True,
    )
    assert updated.status_code == 200
    assert "Updated reminder" in updated.text
    assert "Every 5 minutes" in updated.text

    paused = client.post(f"/cronjobs/{job_id}/pause", follow_redirects=True)
    assert paused.status_code == 200
    assert "Paused" in paused.text
    assert "Resume" in paused.text

    resumed = client.post(f"/cronjobs/{job_id}/resume", follow_redirects=True)
    assert resumed.status_code == 200
    assert "Enabled" in resumed.text

    deleted = client.post(f"/cronjobs/{job_id}/delete", follow_redirects=False)
    assert deleted.status_code == 303
    assert deleted.headers["location"] == "/cronjobs"
    assert client.get(f"/cronjobs/{job_id}").status_code == 404


def test_team_member_cannot_modify_another_creators_job(client, app):
    async def arrange():
        async with app.state.db.uow() as uow:
            await uow._conn.execute(
                "UPDATE team_member SET role='member' WHERE team_id='team_acme' AND member_id='user_bilal'"
            )
            await uow._conn.execute(
                "UPDATE member SET role='team_member' WHERE id='user_bilal'"
            )
            await uow._conn.execute(
                """INSERT INTO scheduled_job
                (id,name,channel_id,instruction,schedule_kind,interval_value,interval_unit,
                 creator_id,enabled,next_run_at,created_at)
                VALUES ('job_locked','Locked','chan_general','No changes','interval',2,
                        'minutes','agent_planner',1,'2027-01-01T00:00:00+00:00',
                        '2026-01-01T00:00:00+00:00')"""
            )

    asyncio.run(arrange())
    assert client.get("/cronjobs/job_locked/edit").status_code == 403
    assert client.post("/cronjobs/job_locked/pause").status_code == 403
    assert client.post("/cronjobs/job_locked/resume").status_code == 403
    assert client.post("/cronjobs/job_locked/delete").status_code == 403
