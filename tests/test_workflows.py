"""Channel-scoped executable workflows."""
from __future__ import annotations

import asyncio
import re
import uuid
from typing import Any


def _workflow_payload(**overrides):
    payload = {
        "name": "deploy_notifier",
        "description": "Notify when deploys are mentioned",
        "channel_id": "chan_general",
        "enabled": True,
        "trigger_type": "message_posted",
        "trigger_config": {},
        "filter_expression": 'contains(text, "deploy")',
        "steps": [
            {
                "id": "step_1",
                "name": "Notify channel",
                "action": "send_message",
                "timeout_seconds": 30,
                "config": {"channel_id": "chan_general", "text": "Deploy detected: {{ text }}"},
            }
        ],
    }
    payload.update(overrides)
    return payload


def _next_message_frame(ws):
    """Receive frames until a chat message frame (ignoring workflow progress events)."""
    while True:
        frame = ws.receive_json()
        if frame.get("type") != "workflow_run_progress" and "body" in frame:
            return frame


def _next_progress_frame(ws, status: str | None = None):
    """Receive frames until a workflow progress event with the requested status."""
    while True:
        frame = ws.receive_json()
        if frame.get("type") == "workflow_run_progress" and (
            status is None or frame.get("status") == status
        ):
            return frame


def test_workflows_are_a_tool_with_a_dedicated_builder(client):
    listing = client.get("/workflows")
    assert listing.status_code == 200
    assert "Workflows" in listing.text
    assert 'href="/workflows/new"' in listing.text
    assert 'href="/workflows"' in client.get("/").text

    builder = client.get("/workflows/new")
    assert builder.status_code == 200
    assert "Create Workflow" in builder.text
    assert "Message Posted" in builder.text
    assert "Reaction Added" in builder.text
    assert "Diff Posted" in builder.text
    assert "Webhook" in builder.text
    assert "Schedule" in builder.text
    for action in (
        "Send Message", "Send DM", "Call Webhook", "Call MCP Tool", "Request Approval",
        "Add Reaction", "Set Channel Topic", "Delay",
    ):
        assert action in builder.text
    assert "Trigger config (JSON)" not in builder.text
    assert 'id="schedule-config"' in builder.text
    assert 'name="schedule_cron"' in builder.text
    assert 'placeholder="e.g. 0 9 * * 1-5 (weekdays at 9am UTC)"' in builder.text
    assert 'name="schedule_interval"' in builder.text
    assert 'placeholder="e.g. 1h, 30m"' in builder.text
    assert "Provide either a cron expression or a simple interval." in builder.text
    assert 'id="webhook-config"' in builder.text
    assert "A unique webhook URL will be generated when the workflow is created." in builder.text


def test_message_posted_workflow_filters_executes_and_logs(client, app):
    created = client.post("/workflows", json=_workflow_payload())
    assert created.status_code == 201
    workflow_id = created.json()["id"]

    with client.websocket_connect("/channels/chan_general/ws", headers={"Origin": "http://testserver"}) as ws:
        ws.send_json({"body": "please deploy api"})
        inbound = ws.receive_json()
        assert inbound["body"] == "please deploy api"
        assert _next_progress_frame(ws, "succeeded")["step_id"] == "step_1"
        assert _next_progress_frame(ws, "completed")["run_status"] == "succeeded"

    messages = client.get("/channels/chan_general/messages").json()
    assert any(message["body"] == "Deploy detected: please deploy api" for message in messages)

    runs = client.get(f"/workflows/{workflow_id}/runs")
    assert runs.status_code == 200
    assert runs.json()[0]["status"] == "succeeded"
    assert runs.json()[0]["step_results"][0]["step_id"] == "step_1"


def test_non_matching_message_does_not_run_workflow(client):
    workflow_id = client.post("/workflows", json=_workflow_payload()).json()["id"]
    with client.websocket_connect("/channels/chan_general/ws", headers={"Origin": "http://testserver"}) as ws:
        ws.send_json({"body": "hello team"})
        assert ws.receive_json()["body"] == "hello team"
    assert client.get(f"/workflows/{workflow_id}/runs").json() == []


def test_workflow_schema_and_repository_round_trip(client, app):
    created = client.post("/workflows", json=_workflow_payload(enabled=False))
    assert created.status_code == 201
    workflow_id = created.json()["id"]

    async def load():
        async with app.state.db.uow() as uow:
            return await uow.workflows.get(workflow_id)

    workflow = asyncio.run(load())
    assert workflow is not None
    assert workflow.name == "deploy_notifier"
    assert workflow.trigger_type == "message_posted"
    assert workflow.steps[0]["action"] == "send_message"
    assert workflow.enabled is False


def test_run_emits_live_per_step_progress_and_persists_state(client, app):
    from crewspace.application.workflows import WorkflowService

    payload = _workflow_payload(
        name="progress_demo",
        filter_expression=None,
        steps=[
            {"id": "first", "action": "send_message", "config": {"text": "step one"}},
            {"id": "second", "action": "add_reaction",
             "config": {"message_id": "{{ message_id }}", "emoji": "🚀"}},
        ],
    )

    async def build():
        async with app.state.db.uow() as uow:
            return await WorkflowService().create(
                uow, creator_id="Bilal", data=payload
            )

    workflow = asyncio.run(build())

    events: list[dict] = []

    async def on_progress(event: dict) -> None:
        events.append(event)
        # While step 2 is starting, step 1 must already be persisted as succeeded.
        if event.get("status") == "started" and event.get("step_id") == "second":
            async with app.state.db.uow() as uow:
                live = await uow.workflows.get_run(event["run_id"])
                completed = [r for r in live.step_results if r["status"] == "succeeded"]
                assert any(r["step_id"] == "first" for r in completed)

    async def go():
        async with app.state.db.uow() as uow:
            wf = await uow.workflows.get(workflow.id)
            return await WorkflowService(on_progress=on_progress).run(
                wf, uow, {"text": "hi", "message_id": "msg_x"}
            )

    asyncio.run(go())

    statuses = [(e["step_id"], e["status"]) for e in events]
    assert ("first", "started") in statuses
    assert ("first", "succeeded") in statuses
    assert ("second", "started") in statuses
    assert ("second", "succeeded") in statuses


def test_manual_run_broadcasts_progress_frames_to_channel_and_ui_handles_them(client):
    workflow = client.post("/workflows", json=_workflow_payload(
        name="live_progress",
        filter_expression=None,
        steps=[{"id": "pause", "action": "delay", "config": {"seconds": 0}}],
    )).json()

    with client.websocket_connect(
        "/channels/chan_general/ws", headers={"Origin": "http://testserver"}
    ) as ws:
        response = client.post(f'/workflows/{workflow["id"]}/run')
        assert response.status_code == 200
        started = ws.receive_json()
        step_completed = ws.receive_json()
        run_completed = ws.receive_json()

    assert started["type"] == "workflow_run_progress"
    assert started["run_id"] == response.json()["id"]
    assert started["step_id"] == "pause"
    assert started["status"] == "started"
    assert step_completed["type"] == "workflow_run_progress"
    assert step_completed["status"] == "succeeded"
    assert run_completed["status"] == "completed"
    assert run_completed["run_status"] == "succeeded"
    assert 'data.type==="workflow_run_progress"' in client.get("/").text


def test_all_actions_execute_in_order_and_approval_resumes(client):
    payload = _workflow_payload(
        name="all_actions",
        filter_expression=None,
        steps=[
            {"id": "delay", "action": "delay", "config": {"seconds": 0}},
            {"id": "dm", "action": "send_dm", "config": {"member_id": "agent_planner", "text": "Private {{ text }}"}},
            {"id": "react", "action": "add_reaction", "config": {"message_id": "{{ message_id }}", "emoji": "✅"}},
            {"id": "topic", "action": "set_channel_topic", "config": {"topic": "Automated for {{ text }}"}},
            {"id": "approval", "action": "request_approval", "config": {"prompt": "Ship it?"}},
            {"id": "after", "action": "send_message", "config": {"text": "Approved {{ text }}"}},
        ],
    )
    workflow_id = client.post("/workflows", json=payload).json()["id"]
    with client.websocket_connect("/channels/chan_general/ws", headers={"Origin": "http://testserver"}) as ws:
        ws.send_json({"body": "release"})
        ws.receive_json()
        assert _next_progress_frame(ws, "waiting")["run_status"] == "waiting"

    runs = client.get(f"/workflows/{workflow_id}/runs").json()
    assert runs[0]["status"] == "waiting"
    token = runs[0]["approval_token"]
    inbox = client.get("/workflows")
    assert "Pending approvals" in inbox.text
    assert "Ship it?" in inbox.text
    assert f'/workflows/approvals/{token}/approve' in inbox.text
    assert f'/workflows/approvals/{token}/reject' in inbox.text
    approved = client.post(f"/workflows/approvals/{token}/approve")
    assert approved.status_code == 200
    assert approved.json()["status"] == "succeeded"
    assert any(m["body"] == "Approved release" for m in client.get("/channels/chan_general/messages").json())
    channel_page = client.get("/channels/chan_general")
    assert channel_page.status_code == 200
    assert "Automated for release" in channel_page.text


def test_reaction_diff_and_webhook_triggers(client):
    webhook = None
    for name, trigger in (("reaction_flow", "reaction_added"), ("diff_flow", "diff_posted"), ("hook_flow", "webhook")):
        response = client.post("/workflows", json=_workflow_payload(
            name=name, trigger_type=trigger, filter_expression=None,
            steps=[{"id": "notify", "action": "send_message", "config": {"text": f"{trigger}: {{{{ text }}}}"}}],
        ))
        assert response.status_code == 201
        if trigger == "webhook":
            webhook = response.json()["webhook"]

    with client.websocket_connect("/channels/chan_general/ws", headers={"Origin": "http://testserver"}) as ws:
        ws.send_json({"body": "react to me"})
        message_id = ws.receive_json()["id"]
    assert client.post(f"/channels/chan_general/messages/{message_id}/reactions", json={"emoji": "🚀"}).status_code == 200
    assert client.post("/channels/chan_general/diffs", json={"text": "diff --git a/x b/x"}).status_code == 201
    assert webhook is not None
    assert client.post(
        webhook["url"].replace("http://testserver", ""), json={"text": "external"},
        headers={"X-Webhook-Secret": webhook["secret"]},
    ).status_code == 202
    bodies = [m["body"] for m in client.get("/channels/chan_general/messages").json()]
    assert "reaction_added: 🚀" in bodies
    assert "diff_posted: diff --git a/x b/x" in bodies
    assert "webhook: external" in bodies


def test_schedule_trigger_and_call_webhook_action(client, app):
    from crewspace.application.workflows import WorkflowSchedulerLoop

    calls = []

    class Executor:
        async def call(self, **kwargs):
            calls.append(kwargs)
            return {"status": 204, "body": "ok"}

    created = client.post("/workflows", json=_workflow_payload(
        name="scheduled_hook", trigger_type="schedule", trigger_config={"every_seconds": 60},
        filter_expression=None,
        steps=[{"id": "hook", "action": "call_webhook", "config": {
            "url": "https://example.test/hook", "body": {"channel": "{{ channel_id }}"}
        }}],
    ))
    assert created.status_code == 201
    workflow_id = created.json()["id"]

    async def due():
        async with app.state.db.uow() as uow:
            await uow._conn.execute(
                "UPDATE workflow SET next_run_at=? WHERE id=?",
                ("2000-01-01T00:00:00+00:00", workflow_id),
            )
            await uow.commit()
        return await WorkflowSchedulerLoop(
            app.state.db, webhook_executor=Executor()
        ).run_due_once()

    assert asyncio.run(due()) == 1
    assert calls[0]["url"] == "https://example.test/hook"
    assert calls[0]["body"] == {"channel": "chan_general"}
    assert client.get(f"/workflows/{workflow_id}/runs").json()[0]["status"] == "succeeded"


def test_two_workflow_scheduler_workers_claim_due_occurrence_once(client, app, monkeypatch):
    import datetime as dt

    from crewspace.application.workflows import WorkflowSchedulerLoop, WorkflowService

    workflow = client.post("/workflows", json=_workflow_payload(
        name="claim_once", trigger_type="schedule",
        trigger_config={"interval": "1h"}, filter_expression=None,
    )).json()
    calls: list[str] = []

    async def fake_run(self, claimed, uow, event, **kwargs):
        calls.append(claimed.id)
        await asyncio.sleep(0.05)
        claimed.next_run_at = dt.datetime.now(dt.timezone.utc) + dt.timedelta(hours=1)
        await uow.workflows.update(claimed)
        await uow.commit()

    monkeypatch.setattr(WorkflowService, "run", fake_run)

    async def race():
        async with app.state.db.uow() as uow:
            await uow._conn.execute(
                "UPDATE workflow SET next_run_at=? WHERE id=?",
                ("2000-01-01T00:00:00+00:00", workflow["id"]),
            )
            await uow.commit()
        first = WorkflowSchedulerLoop(app.state.db)
        second = WorkflowSchedulerLoop(app.state.db)
        return await asyncio.gather(first.run_due_once(), second.run_due_once())

    assert sum(asyncio.run(race())) == 1
    assert calls == [workflow["id"]]


def test_schedule_accepts_human_interval_and_rejects_missing_or_ambiguous_schedule(client):
    payload = _workflow_payload(
        name="human_schedule", trigger_type="schedule",
        trigger_config={"interval": "30m"}, filter_expression=None,
    )
    created = client.post("/workflows", json=payload)
    assert created.status_code == 201
    assert created.json()["trigger_config"] == {"interval": "30m", "every_seconds": 1800}

    missing = client.post("/workflows", json={**payload, "name": "missing_schedule", "trigger_config": {}})
    assert missing.status_code == 422
    assert "either a cron expression or a simple interval" in missing.json()["detail"]

    both = client.post("/workflows", json={
        **payload, "name": "ambiguous_schedule",
        "trigger_config": {"cron": "0 9 * * 1-5", "interval": "30m"},
    })
    assert both.status_code == 422

    cron = client.post("/workflows", json={
        **payload, "name": "weekday_schedule",
        "trigger_config": {"cron": "0 9 * * 1-5"},
    })
    assert cron.status_code == 201
    assert cron.json()["trigger_config"] == {"cron": "0 9 * * 1-5"}


def test_step_run_condition_skips_only_the_non_matching_step(client):
    payload = _workflow_payload(
        name="conditional_steps", filter_expression=None,
        steps=[
            {"id": "skip", "action": "send_message",
             "condition": 'str_contains(trigger_text, "deploy")',
             "config": {"text": "should not send"}},
            {"id": "always", "action": "send_message", "config": {"text": "always sent"}},
        ],
    )
    workflow_id = client.post("/workflows", json=payload).json()["id"]
    with client.websocket_connect("/channels/chan_general/ws", headers={"Origin": "http://testserver"}) as ws:
        ws.send_json({"body": "hello team"})
        ws.receive_json()
        assert _next_progress_frame(ws, "completed")["run_status"] == "succeeded"
    messages = client.get("/channels/chan_general/messages").json()
    assert not any(item["body"] == "should not send" for item in messages)
    assert any(item["body"] == "always sent" for item in messages)
    results = client.get(f"/workflows/{workflow_id}/runs").json()[0]["step_results"]
    assert results[0]["status"] == "skipped"


def test_webhook_creation_returns_one_time_credentials_and_requires_secret(client):
    payload = _workflow_payload(
        name="incoming_hook", trigger_type="webhook", trigger_config={}, filter_expression=None,
        steps=[{"id": "notify", "action": "send_message", "config": {
            "channel_id": "chan_general",
            "text": "Hook {{trigger.author}}: {{trigger.pesan}}",
        }}],
    )
    created = client.post("/workflows", json=payload)
    assert created.status_code == 201
    body = created.json()
    assert body["webhook"]["url"].startswith("http://testserver/hooks/")
    assert body["webhook"]["secret"]
    hook_id = body["webhook"]["url"].rsplit("/", 1)[-1]

    assert client.post(f"/hooks/{hook_id}", json={"text": "external"}).status_code == 401
    accepted = client.post(
        f"/hooks/{hook_id}", json={"author": "Nasa", "pesan": "Lagi nyoba"},
        headers={"X-Webhook-Secret": body["webhook"]["secret"]},
    )
    assert accepted.status_code == 202
    assert any(
        m["body"] == "Hook Nasa: Lagi nyoba"
        for m in client.get("/channels/chan_general/messages").json()
    )

    listing = client.get("/workflows/api").json()
    saved = next(item for item in listing if item["id"] == body["id"])
    assert "secret" not in saved["trigger_config"]
    assert "secret_hash" not in saved["trigger_config"]


def test_webhook_send_message_broadcasts_to_an_open_channel_socket(client):
    payload = _workflow_payload(
        name="live_hook", trigger_type="webhook", trigger_config={}, filter_expression=None,
        steps=[{"id": "notify", "action": "send_message", "config": {
            "channel_id": "chan_general",
            "text": "Live {{trigger.author}}: {{trigger.pesan}}",
        }}],
    )
    webhook = client.post("/workflows", json=payload).json()["webhook"]
    hook_path = webhook["url"].replace("http://testserver", "")

    with client.websocket_connect(
        "/channels/chan_general/ws", headers={"Origin": "http://testserver"}
    ) as ws:
        response = client.post(
            hook_path, json={"author": "Nasa", "pesan": "Lagi nyoba"},
            headers={"X-Webhook-Secret": webhook["secret"]},
        )
        assert response.status_code == 202
        live = _next_message_frame(ws)
        _next_progress_frame(ws, "succeeded")
        _next_progress_frame(ws, "completed")

    assert live["body"] == "Live Nasa: Lagi nyoba"
    assert live["channel_id"] == "chan_general"
    assert live["author_id"] == "agent_crewspace"
    assert live["author_name"] == "Crewspace"
    assert live["author_kind"] == "agent"


def test_message_trigger_templates_and_broadcasts_without_refresh(client):
    client.post("/workflows", json=_workflow_payload(
        name="live_message", filter_expression=None,
        steps=[{"id": "notify", "action": "send_message", "config": {
            "text": "Message by {{trigger.author_id}}: {{trigger.text}}",
        }}],
    ))
    with client.websocket_connect(
        "/channels/chan_general/ws", headers={"Origin": "http://testserver"}
    ) as ws:
        ws.send_json({"body": "hello realtime"})
        human = ws.receive_json()
        automated = _next_message_frame(ws)
        _next_progress_frame(ws, "succeeded")
        _next_progress_frame(ws, "completed")
    assert human["body"] == "hello realtime"
    assert automated["body"] == "Message by user_bilal: hello realtime"


def test_reaction_trigger_templates_and_broadcasts_without_refresh(client):
    client.post("/workflows", json=_workflow_payload(
        name="live_reaction", trigger_type="reaction_added", filter_expression=None,
        steps=[{"id": "notify", "action": "send_message", "config": {
            "text": "Reaction {{trigger.emoji}} by {{trigger.member_id}}",
        }}],
    ))
    with client.websocket_connect(
        "/channels/chan_general/ws", headers={"Origin": "http://testserver"}
    ) as ws:
        ws.send_json({"body": "react here"})
        message_id = ws.receive_json()["id"]
        response = client.post(
            f"/channels/chan_general/messages/{message_id}/reactions",
            json={"emoji": "🚀"},
        )
        assert response.status_code == 200
        automated = _next_message_frame(ws)
        _next_progress_frame(ws, "succeeded")
        _next_progress_frame(ws, "completed")
    assert automated["body"] == "Reaction 🚀 by user_bilal"


def test_diff_trigger_templates_and_broadcasts_without_refresh(client):
    client.post("/workflows", json=_workflow_payload(
        name="live_diff", trigger_type="diff_posted", filter_expression=None,
        steps=[{"id": "notify", "action": "send_message", "config": {
            "text": "Diff by {{trigger.author_id}}: {{trigger.text}}",
        }}],
    ))
    with client.websocket_connect(
        "/channels/chan_general/ws", headers={"Origin": "http://testserver"}
    ) as ws:
        response = client.post(
            "/channels/chan_general/diffs", json={"text": "diff --git a/a b/a"}
        )
        assert response.status_code == 201
        automated = _next_message_frame(ws)
        _next_progress_frame(ws, "succeeded")
        _next_progress_frame(ws, "completed")
    assert automated["body"] == "Diff by user_bilal: diff --git a/a b/a"


def test_schedule_trigger_templates_and_emits_message_callback(client, app):
    from crewspace.application.workflows import WorkflowSchedulerLoop

    created = client.post("/workflows", json=_workflow_payload(
        name="live_schedule", trigger_type="schedule",
        trigger_config={"interval": "1h"}, filter_expression=None,
        steps=[{"id": "notify", "action": "send_message", "config": {
            "text": "Scheduled {{trigger.scheduled_at}} in {{trigger.channel_id}}",
        }}],
    )).json()
    emitted = []

    async def due():
        async with app.state.db.uow() as uow:
            await uow._conn.execute(
                "UPDATE workflow SET next_run_at=? WHERE id=?",
                ("2000-01-01T00:00:00+00:00", created["id"]),
            )
            await uow.commit()

        async def capture(message):
            emitted.append(message)

        return await WorkflowSchedulerLoop(
            app.state.db, on_message=capture
        ).run_due_once()

    assert asyncio.run(due()) == 1
    assert len(emitted) == 1
    assert emitted[0].body.startswith("Scheduled ")
    assert emitted[0].body.endswith(" in chan_general")


def test_workflow_list_exposes_run_edit_toggle_and_delete_actions(client):
    workflow = client.post("/workflows", json=_workflow_payload(
        name="managed_flow"
    )).json()
    page = client.get("/workflows")
    assert page.status_code == 200
    assert f'/workflows/{workflow["id"]}/run' in page.text
    browser_run = client.post(
        f'/workflows/{workflow["id"]}/run?redirect=1', follow_redirects=False
    )
    assert browser_run.status_code == 303
    assert browser_run.headers["location"] == "/workflows"
    assert f'/workflows/{workflow["id"]}/edit' in page.text
    assert f'/workflows/{workflow["id"]}/disable' in page.text
    assert f'/workflows/{workflow["id"]}/delete' in page.text
    assert 'class="list workflow-list"' in page.text
    assert '.workflow-list .workflow:last-child .menu-popover' in page.text



def test_workflow_run_audit_export_returns_checkpointed_document(client, app):
    workflow = client.post("/workflows", json=_workflow_payload(
        name="auditable", filter_expression='contains(text, "never")',
        steps=[
            {"id": "first", "action": "send_message", "config": {"text": "step one"}},
            {"id": "second", "action": "add_reaction",
             "config": {"message_id": "{{ message_id }}", "emoji": "🚀"}},
        ],
    )).json()
    run = client.post(f'/workflows/{workflow["id"]}/run').json()
    assert run["status"] == "succeeded"

    response = client.get(f'/workflows/{workflow["id"]}/runs/{run["id"]}/export')
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/json")
    doc = response.json()
    assert doc["workflow_id"] == workflow["id"]
    assert doc["run_id"] == run["id"]
    assert doc["trigger_type"] == "manual"
    assert doc["status"] == "succeeded"
    assert "started_at" in doc and "finished_at" in doc
    assert doc["trigger_payload"] == run["event"]
    assert [s["step_id"] for s in doc["steps"]] == ["first", "second"]
    assert doc["steps"][0]["status"] == "succeeded"
    assert doc["lineage"]["attempt"] == 1
    assert doc["lineage"]["parent_run_id"] is None


def test_workflow_run_audit_export_csv_flattens_steps(client, app):
    workflow = client.post("/workflows", json=_workflow_payload(
        name="csv_auditable", filter_expression='contains(text, "never")',
        steps=[
            {"id": "first", "action": "send_message", "config": {"text": "step one"}},
            {"id": "second", "action": "add_reaction",
             "config": {"message_id": "{{ message_id }}", "emoji": "🚀"}},
        ],
    )).json()
    run = client.post(f'/workflows/{workflow["id"]}/run').json()
    assert run["status"] == "succeeded"

    response = client.get(
        f'/workflows/{workflow["id"]}/runs/{run["id"]}/export?format=csv'
    )
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/csv")
    assert "attachment" in response.headers["content-disposition"]
    lines = response.text.strip().splitlines()
    assert lines[0] == "step_id,status,workflow_id,run_id"
    assert "first,succeeded" in lines[1]
    assert "second,succeeded" in lines[2]


def test_workflow_run_audit_export_forbidden_for_non_owner(client, app):
    workflow = client.post("/workflows", json=_workflow_payload(
        name="owned_by_bilal", filter_expression='contains(text, "never")',
        steps=[{"id": "only", "action": "send_message", "config": {"text": "x"}}],
    )).json()
    run = client.post(f'/workflows/{workflow["id"]}/run').json()

    # Viewer is a channel member but not the creator and not a superadmin.
    suffix = uuid.uuid4().hex[:8]
    viewer_name = f"Audit Viewer {suffix}"
    created = client.post("/management/humans", data={
        "name": viewer_name, "password": "member123", "team_id": "team_acme",
        "role": "member",
    })
    assert created.status_code == 200
    management = client.get("/management/channels/chan_general/members")
    member_match = re.search(
        rf'<option value="(user_[^"]+)">{re.escape(viewer_name)}', management.text
    )
    assert member_match is not None
    assert client.post(
        "/management/channels/chan_general/members",
        data={"member_id": member_match.group(1)},
    ).status_code == 200
    client.post("/auth/logout")
    assert client.post("/auth/login", data={
        "username": viewer_name, "password": "member123",
    }).status_code == 200
    response = client.get(f'/workflows/{workflow["id"]}/runs/{run["id"]}/export')
    assert response.status_code == 403
    client.post("/auth/logout")
    assert client.post("/auth/login", data={
        "username": "Bilal", "password": "admin123",
    }).status_code == 200


def test_workflow_detail_shows_audit_export_links_for_each_run(client, app):
    workflow = client.post("/workflows", json=_workflow_payload(
        name="auditable_detail", filter_expression='contains(text, "never")',
        steps=[{"id": "only", "action": "send_message", "config": {"text": "x"}}],
    )).json()
    run = client.post(f'/workflows/{workflow["id"]}/run').json()
    assert run["status"] == "succeeded"

    detail = client.get(f'/workflows/{workflow["id"]}')
    assert detail.status_code == 200
    body = detail.text
    assert f'/workflows/{workflow["id"]}/runs/{run["id"]}/export?format=csv' in body
    assert f'/workflows/{workflow["id"]}/runs/{run["id"]}/export?format=json' in body
    assert 'Audit' in body


def test_workflow_creator_can_run_now_with_realtime_delivery_and_history(client):
    workflow = client.post("/workflows", json=_workflow_payload(
        name="manual_flow", filter_expression='contains(text, "never")',
        steps=[{"id": "notify", "action": "send_message", "config": {
            "text": "Manual run by {{trigger.initiated_by}}",
        }}],
    )).json()

    with client.websocket_connect(
        "/channels/chan_general/ws", headers={"Origin": "http://testserver"}
    ) as ws:
        response = client.post(f'/workflows/{workflow["id"]}/run')
        assert response.status_code == 200
        live = _next_message_frame(ws)
        _next_progress_frame(ws, "succeeded")
        _next_progress_frame(ws, "completed")

    assert response.json()["status"] == "succeeded"
    assert response.json()["trigger_type"] == "manual"
    assert live["body"] == "Manual run by user_bilal"
    runs = client.get(f'/workflows/{workflow["id"]}/runs').json()
    assert runs[0]["id"] == response.json()["id"]
    assert runs[0]["event"]["initiated_by"] == "user_bilal"


def test_workflow_detail_shows_definition_actions_and_step_run_history(client):
    workflow = client.post("/workflows", json=_workflow_payload(
        name="observable_flow", filter_expression=None,
        steps=[
            {"id": "notify", "name": "Notify channel", "action": "send_message",
             "config": {"text": "Observed {{trigger.initiated_by}}"}},
            {"id": "skip", "name": "Deploy only", "action": "send_message",
             "condition": 'str_contains(trigger_text, "deploy")',
             "config": {"text": "not sent"}},
        ],
    )).json()
    run = client.post(f'/workflows/{workflow["id"]}/run').json()

    listing = client.get("/workflows")
    assert f'href="/workflows/{workflow["id"]}"' in listing.text
    detail = client.get(f'/workflows/{workflow["id"]}')
    assert detail.status_code == 200
    assert "observable_flow" in detail.text
    assert "Message Posted" in detail.text
    assert "Notify channel" in detail.text
    assert "Deploy only" in detail.text
    assert f'action="/workflows/{workflow["id"]}/run?redirect=detail"' in detail.text
    assert f'href="/workflows/{workflow["id"]}/edit"' in detail.text
    assert run["id"] in detail.text
    assert "Manual" in detail.text
    assert "Succeeded" in detail.text
    assert "Notify channel" in detail.text
    assert "Skipped" in detail.text
    assert "user_bilal" in detail.text


def test_workflow_detail_hides_management_actions_from_channel_viewer(client):
    workflow = client.post("/workflows", json=_workflow_payload(
        name="viewable_flow"
    )).json()
    created = client.post(
        "/management/humans",
        data={"name": "Run Viewer", "password": "temporary-password", "team_id": "team_acme"},
    )
    assert created.status_code == 200
    management = client.get("/management/channels/chan_general/members")
    member_match = re.search(r'<option value="(user_[^"]+)">Run Viewer', management.text)
    assert member_match is not None
    assert client.post(
        "/management/channels/chan_general/members",
        data={"member_id": member_match.group(1)},
    ).status_code == 200
    client.post("/auth/logout")
    assert client.post(
        "/auth/login", data={"username": "Run Viewer", "password": "temporary-password"}
    ).status_code == 200

    detail = client.get(f'/workflows/{workflow["id"]}')
    assert detail.status_code == 200
    assert "viewable_flow" in detail.text
    assert f'/workflows/{workflow["id"]}/edit' not in detail.text
    assert f'/workflows/{workflow["id"]}/run' not in detail.text


def test_creator_retries_failed_run_from_failed_step_with_lineage(client):
    workflow = client.post("/workflows", json=_workflow_payload(
        name="retryable",
        steps=[
            {"id": "first", "action": "send_message", "config": {"text": "first side effect"}},
            {"id": "second", "action": "send_message", "config": {"text": ""}},
        ],
    )).json()
    original = client.post(f'/workflows/{workflow["id"]}/run').json()
    assert original["status"] == "failed"
    assert original["current_step"] == 1

    updated = _workflow_payload(
        name="retryable",
        steps=[
            {"id": "first", "action": "send_message", "config": {"text": "first side effect"}},
            {"id": "second", "action": "send_message", "config": {"text": "recovered {{trigger.initiated_by}}"}},
        ],
    )
    assert client.put(f'/workflows/{workflow["id"]}', json=updated).status_code == 200

    retried = client.post(
        f'/workflows/{workflow["id"]}/runs/{original["id"]}/retry'
    )
    assert retried.status_code == 200
    attempt = retried.json()
    assert attempt["status"] == "succeeded"
    assert attempt["parent_run_id"] == original["id"]
    assert attempt["root_run_id"] == original["id"]
    assert attempt["attempt"] == 2
    assert attempt["event"] == original["event"]
    assert [item["step_id"] for item in attempt["step_results"]] == ["second"]

    messages = [item["body"] for item in client.get("/channels/chan_general/messages").json()]
    assert messages.count("first side effect") == 1
    assert messages.count("recovered user_bilal") == 1
    original_after = next(
        item for item in client.get(f'/workflows/{workflow["id"]}/runs').json()
        if item["id"] == original["id"]
    )
    assert original_after["status"] == "failed"

    detail = client.get(f'/workflows/{workflow["id"]}')
    assert detail.status_code == 200
    assert f'/workflows/{workflow["id"]}/runs/{original["id"]}/retry' in detail.text
    assert f'Retry of {original["id"]}' in detail.text


def test_retry_of_retry_reconstructs_outputs_from_full_lineage(client):
    name = f"retry_chain_{uuid.uuid4().hex[:8]}"
    workflow = client.post("/workflows", json=_workflow_payload(
        name=name,
        steps=[
            {"id": "first", "action": "send_message", "config": {"text": "alpha"}},
            {"id": "second", "action": "send_message", "config": {"text": ""}},
            {"id": "third", "action": "send_message", "config": {"text": ""}},
        ],
    )).json()
    original = client.post(f'/workflows/{workflow["id"]}/run').json()
    assert original["current_step"] == 1

    assert client.put(f'/workflows/{workflow["id"]}', json=_workflow_payload(
        name=name,
        steps=[
            {"id": "first", "action": "send_message", "config": {"text": "alpha"}},
            {"id": "second", "action": "send_message", "config": {"text": "beta {{first.text}}"}},
            {"id": "third", "action": "send_message", "config": {"text": ""}},
        ],
    )).status_code == 200
    second = client.post(
        f'/workflows/{workflow["id"]}/runs/{original["id"]}/retry'
    ).json()
    assert second["status"] == "failed"
    assert second["current_step"] == 2

    assert client.put(f'/workflows/{workflow["id"]}', json=_workflow_payload(
        name=name,
        steps=[
            {"id": "first", "action": "send_message", "config": {"text": "alpha"}},
            {"id": "second", "action": "send_message", "config": {"text": "beta {{first.text}}"}},
            {"id": "third", "action": "send_message", "config": {
                "text": "gamma {{first.text}} / {{second.text}}",
            }},
        ],
    )).status_code == 200
    third = client.post(
        f'/workflows/{workflow["id"]}/runs/{second["id"]}/retry'
    ).json()
    assert third["status"] == "succeeded"
    assert third["attempt"] == 3
    assert third["parent_run_id"] == second["id"]
    messages = [item["body"] for item in client.get("/channels/chan_general/messages").json()]
    assert "gamma alpha / beta alpha" in messages


def test_retry_requires_failed_run_and_workflow_manager(client):
    suffix = uuid.uuid4().hex[:8]
    workflow = client.post(
        "/workflows", json=_workflow_payload(name=f"retry_guard_{suffix}")
    ).json()
    succeeded = client.post(f'/workflows/{workflow["id"]}/run').json()
    assert client.post(
        f'/workflows/{workflow["id"]}/runs/{succeeded["id"]}/retry'
    ).status_code == 409

    viewer_name = f"Intern {suffix}"
    created = client.post("/management/humans", data={
        "name": viewer_name, "password": "intern123", "team_id": "team_acme",
        "role": "member",
    })
    assert created.status_code == 200
    management = client.get("/management/channels/chan_general/members")
    member_match = re.search(
        rf'<option value="(user_[^"]+)">{re.escape(viewer_name)}', management.text
    )
    assert member_match is not None
    assert client.post(
        "/management/channels/chan_general/members",
        data={"member_id": member_match.group(1)},
    ).status_code == 200
    client.post("/auth/logout")
    assert client.post("/auth/login", data={
        "username": viewer_name, "password": "intern123",
    }).status_code == 200
    assert client.post(
        f'/workflows/{workflow["id"]}/runs/{succeeded["id"]}/retry'
    ).status_code == 403


def test_workflow_can_be_edited_and_updated_definition_executes(client):
    workflow = client.post("/workflows", json=_workflow_payload(
        name="editable_flow", filter_expression=None
    )).json()
    edit = client.get(f'/workflows/{workflow["id"]}/edit')
    assert edit.status_code == 200
    assert "Edit Workflow" in edit.text
    assert 'value="editable_flow"' in edit.text

    updated_payload = _workflow_payload(
        name="edited_flow", description="Updated definition", filter_expression=None,
        steps=[{"id": "updated", "action": "send_message", "config": {
            "text": "Edited {{trigger.text}}",
        }}],
    )
    updated = client.put(f'/workflows/{workflow["id"]}', json=updated_payload)
    assert updated.status_code == 200
    assert updated.json()["name"] == "edited_flow"

    with client.websocket_connect(
        "/channels/chan_general/ws", headers={"Origin": "http://testserver"}
    ) as ws:
        ws.send_json({"body": "works now"})
        assert ws.receive_json()["body"] == "works now"
        assert _next_message_frame(ws)["body"] == "Edited works now"
        _next_progress_frame(ws, "succeeded")
        _next_progress_frame(ws, "completed")


def test_workflow_can_be_disabled_and_enabled(client):
    workflow = client.post("/workflows", json=_workflow_payload(
        name="toggle_flow", filter_expression=None
    )).json()
    disabled = client.post(
        f'/workflows/{workflow["id"]}/disable', follow_redirects=False
    )
    assert disabled.status_code == 303
    saved = next(item for item in client.get("/workflows/api").json() if item["id"] == workflow["id"])
    assert saved["enabled"] is False

    with client.websocket_connect(
        "/channels/chan_general/ws", headers={"Origin": "http://testserver"}
    ) as ws:
        ws.send_json({"body": "disabled deploy"})
        assert ws.receive_json()["body"] == "disabled deploy"
    assert client.get(f'/workflows/{workflow["id"]}/runs').json() == []

    enabled = client.post(
        f'/workflows/{workflow["id"]}/enable', follow_redirects=False
    )
    assert enabled.status_code == 303
    saved = next(item for item in client.get("/workflows/api").json() if item["id"] == workflow["id"])
    assert saved["enabled"] is True


def test_workflow_delete_requires_confirmation_and_removes_definition(client):
    workflow = client.post("/workflows", json=_workflow_payload(
        name="delete_flow"
    )).json()
    confirmation = client.get(f'/workflows/{workflow["id"]}/delete')
    assert confirmation.status_code == 200
    assert "Delete workflow" in confirmation.text
    assert "delete_flow" in confirmation.text

    mismatch = client.post(
        f'/workflows/{workflow["id"]}/delete', data={"confirmation": "wrong"}
    )
    assert mismatch.status_code == 422
    deleted = client.post(
        f'/workflows/{workflow["id"]}/delete',
        data={"confirmation": "delete_flow"}, follow_redirects=False,
    )
    assert deleted.status_code == 303
    assert all(item["id"] != workflow["id"] for item in client.get("/workflows/api").json())


def test_non_creator_channel_member_cannot_manage_workflow(client):
    workflow = client.post("/workflows", json=_workflow_payload(
        name="owned_flow"
    )).json()
    created = client.post(
        "/management/humans",
        data={
            "name": "Workflow Viewer",
            "password": "temporary-password",
            "team_id": "team_acme",
        },
    )
    assert created.status_code == 200
    management = client.get("/management/channels/chan_general/members")
    member_match = re.search(
        r'<option value="(user_[^"]+)">Workflow Viewer', management.text
    )
    assert member_match is not None
    member_id = member_match.group(1)
    assigned = client.post(
        "/management/channels/chan_general/members", data={"member_id": member_id}
    )
    assert assigned.status_code == 200
    client.post("/auth/logout")
    login = client.post(
        "/auth/login",
        data={"username": "Workflow Viewer", "password": "temporary-password"},
    )
    assert login.status_code == 200

    page = client.get("/workflows")
    assert page.status_code == 200
    assert "owned_flow" in page.text
    assert f'/workflows/{workflow["id"]}/edit' not in page.text
    assert client.get(f'/workflows/{workflow["id"]}/edit').status_code == 403
    assert client.put(
        f'/workflows/{workflow["id"]}', json=_workflow_payload(name="stolen_flow")
    ).status_code == 403
    assert client.post(f'/workflows/{workflow["id"]}/run').status_code == 403
    assert client.post(f'/workflows/{workflow["id"]}/disable').status_code == 403
    assert client.get(f'/workflows/{workflow["id"]}/delete').status_code == 403
    assert client.post(
        f'/workflows/{workflow["id"]}/delete', data={"confirmation": "owned_flow"}
    ).status_code == 403


async def _seed_approved_mcp_tool(app, *, connection_id, tool_name, enabled=True):
    import datetime as _dt
    from crewspace.domain.entities import McpConnection, McpDiscoveredTool

    connection = McpConnection(
        id=connection_id, name=connection_id, namespace=f"ns_{connection_id}",
        transport="streamable_http", endpoint_or_command="https://mcp.example.com/mcp",
        enabled=enabled, auth_secret_ref=None, created_by="user_bilal",
        created_at=_dt.datetime(2026, 1, 1, tzinfo=_dt.timezone.utc),
        updated_at=_dt.datetime(2026, 1, 1, tzinfo=_dt.timezone.utc),
    )
    tool = McpDiscoveredTool(
        connection_id=connection_id, tool_name=tool_name,
        description="demo", input_schema={"type": "object", "properties": {}},
        schema_hash="sha256:demo",
        discovered_at=_dt.datetime(2026, 1, 1, tzinfo=_dt.timezone.utc),
        approval_state="approved",
    )
    async with app.state.db.uow() as uow:
        await uow.mcp_connections.create(connection)
        await uow.mcp_connections.upsert_discovered_tool(tool)
        await uow.mcp_connections.set_tool_approval_state(
            connection_id, tool_name, "approved"
        )
        await uow.commit()


class FakeMcpExecutor:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def call_tool(self, connection, tool_name, arguments):
        self.calls.append({
            "connection_id": connection.id, "tool_name": tool_name,
            "arguments": arguments,
        })
        return {"ok": True, "echo": arguments}


async def _seed_pending_mcp_tool(app, *, connection_id, tool_name):
    import datetime as _dt
    from crewspace.domain.entities import McpDiscoveredTool

    pending = McpDiscoveredTool(
        connection_id=connection_id, tool_name=tool_name,
        description="demo", input_schema={"type": "object", "properties": {}},
        schema_hash="sha256:demo2",
        discovered_at=_dt.datetime(2026, 1, 1, tzinfo=_dt.timezone.utc),
        approval_state="pending",
    )
    async with app.state.db.uow() as uow:
        await uow.mcp_connections.upsert_discovered_tool(pending)
        await uow.commit()


def test_workflow_step_calls_approved_mcp_tool_and_records_output(
    client, app, monkeypatch,
):
    import crewspace.api.routers.chat as chat_router

    asyncio.run(_seed_approved_mcp_tool(app, connection_id="mcp_demo", tool_name="ping"))
    executor = FakeMcpExecutor()
    monkeypatch.setattr(
        chat_router, "ExternalMcpToolExecutor", lambda: executor,
    )
    workflow = client.post("/workflows", json=_workflow_payload(
        name="mcp_step",
        filter_expression=None,
        steps=[{
            "id": "invoke", "action": "call_mcp_tool",
            "config": {"connection_id": "mcp_demo", "tool_name": "ping",
                       "arguments": {"input": "{{ text }}"}},
        }],
    )).json()
    with client.websocket_connect("/channels/chan_general/ws", headers={"Origin": "http://testserver"}) as ws:
        ws.send_json({"body": "trigger payload"})
        assert ws.receive_json()["body"] == "trigger payload"
        assert _next_progress_frame(ws, "completed")["run_status"] == "succeeded"
    run = client.get(f'/workflows/{workflow["id"]}/runs').json()[0]
    assert run["status"] == "succeeded"
    assert run["step_results"][0]["status"] == "succeeded"
    assert run["step_results"][0]["output"] == {
        "connection_id": "mcp_demo",
        "tool_name": "ping",
        "result": {"ok": True, "echo": {"input": "trigger payload"}},
    }
    assert executor.calls == [{
        "connection_id": "mcp_demo", "tool_name": "ping",
        "arguments": {"input": "trigger payload"},
    }]


def test_workflow_step_rejects_unapproved_mcp_tool(client, app):
    asyncio.run(_seed_approved_mcp_tool(app, connection_id="mcp_demo", tool_name="ping"))
    asyncio.run(_seed_pending_mcp_tool(
        app, connection_id="mcp_demo", tool_name="unapproved"
    ))
    workflow = client.post("/workflows", json=_workflow_payload(
        name="mcp_unapproved",
        filter_expression=None,
        steps=[{
            "id": "invoke", "action": "call_mcp_tool",
            "config": {"connection_id": "mcp_demo", "tool_name": "unapproved",
                       "arguments": {"input": "x"}},
        }],
    )).json()
    with client.websocket_connect("/channels/chan_general/ws", headers={"Origin": "http://testserver"}) as ws:
        ws.send_json({"body": "go"})
        assert ws.receive_json()["body"] == "go"
        assert _next_progress_frame(ws, "completed")["run_status"] == "failed"
    run = client.get(f'/workflows/{workflow["id"]}/runs').json()[0]
    assert run["status"] == "failed"
    assert "not approved" in run["error"]


def test_workflow_step_fails_when_mcp_connection_missing(client):
    workflow = client.post("/workflows", json=_workflow_payload(
        name="mcp_missing",
        filter_expression=None,
        steps=[{
            "id": "invoke", "action": "call_mcp_tool",
            "config": {"connection_id": "mcp_absent", "tool_name": "ping",
                       "arguments": {}},
        }],
    )).json()
    with client.websocket_connect("/channels/chan_general/ws", headers={"Origin": "http://testserver"}) as ws:
        ws.send_json({"body": "go"})
        assert ws.receive_json()["body"] == "go"
        assert _next_progress_frame(ws, "completed")["run_status"] == "failed"
    run = client.get(f'/workflows/{workflow["id"]}/runs').json()[0]
    assert run["status"] == "failed"
    assert "connection" in run["error"].lower()


def test_workflow_step_rejects_disabled_mcp_connection(client, app):
    asyncio.run(_seed_approved_mcp_tool(
        app, connection_id="mcp_disabled", tool_name="ping", enabled=False
    ))
    workflow = client.post("/workflows", json=_workflow_payload(
        name="mcp_disabled",
        filter_expression=None,
        steps=[{
            "id": "invoke", "action": "call_mcp_tool",
            "config": {"connection_id": "mcp_disabled", "tool_name": "ping",
                       "arguments": {}},
        }],
    )).json()
    with client.websocket_connect(
        "/channels/chan_general/ws", headers={"Origin": "http://testserver"}
    ) as ws:
        ws.send_json({"body": "go"})
        assert ws.receive_json()["body"] == "go"
        assert _next_progress_frame(ws, "completed")["run_status"] == "failed"
    run = client.get(f'/workflows/{workflow["id"]}/runs').json()[0]
    assert run["status"] == "failed"
    assert "disabled" in run["error"].lower()


def test_workflow_step_fails_closed_when_no_mcp_executor(client, app, monkeypatch):
    import crewspace.api.routers.chat as chat_router

    asyncio.run(_seed_approved_mcp_tool(app, connection_id="mcp_demo", tool_name="ping"))
    monkeypatch.setattr(chat_router, "ExternalMcpToolExecutor", lambda: None)
    workflow = client.post("/workflows", json=_workflow_payload(
        name="mcp_noexec",
        filter_expression=None,
        steps=[{
            "id": "invoke", "action": "call_mcp_tool",
            "config": {"connection_id": "mcp_demo", "tool_name": "ping", "arguments": {}},
        }],
    )).json()
    with client.websocket_connect("/channels/chan_general/ws", headers={"Origin": "http://testserver"}) as ws:
        ws.send_json({"body": "go"})
        assert ws.receive_json()["body"] == "go"
        assert _next_progress_frame(ws, "completed")["run_status"] == "failed"
    run = client.get(f'/workflows/{workflow["id"]}/runs').json()[0]
    assert run["status"] == "failed"
    assert "unavailable" in run["error"].lower()


def test_non_superadmin_cannot_create_mcp_tool_workflow(client):
    suffix = uuid.uuid4().hex[:8]
    viewer_name = f"MCP Member {suffix}"
    created = client.post("/management/humans", data={
        "name": viewer_name, "password": "member123", "team_id": "team_acme",
        "role": "member",
    })
    assert created.status_code == 200
    management = client.get("/management/channels/chan_general/members")
    member_match = re.search(
        rf'<option value="(user_[^"]+)">{re.escape(viewer_name)}', management.text
    )
    assert member_match is not None
    assert client.post(
        "/management/channels/chan_general/members",
        data={"member_id": member_match.group(1)},
    ).status_code == 200
    client.post("/auth/logout")
    assert client.post("/auth/login", data={
        "username": viewer_name, "password": "member123",
    }).status_code == 200

    response = client.post("/workflows", json=_workflow_payload(
        name=f"member_mcp_{suffix}", filter_expression=None,
        steps=[{
            "id": "invoke", "action": "call_mcp_tool",
            "config": {"connection_id": "mcp_demo", "tool_name": "ping",
                       "arguments": {}},
        }],
    ))
    assert response.status_code == 403


def test_workflow_builder_exposes_call_mcp_tool_action(client):
    builder = client.get("/workflows/new")
    assert "Call MCP Tool" in builder.text
