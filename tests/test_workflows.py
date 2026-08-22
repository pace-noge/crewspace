"""Channel-scoped executable workflows."""
from __future__ import annotations

import asyncio
import re


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
        "Send Message", "Send DM", "Call Webhook", "Request Approval",
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


def test_schedule_trigger_and_call_webhook_action(client, app, monkeypatch):
    import urllib.request
    from crewspace.application.workflows import WorkflowSchedulerLoop

    calls = []
    class Response:
        status = 204
        def __enter__(self): return self
        def __exit__(self, *args): return None
        def read(self, limit): return b"ok"
    def fake_urlopen(request, timeout):
        calls.append((request.full_url, request.data, timeout))
        return Response()
    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)

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
        return await WorkflowSchedulerLoop(app.state.db).run_due_once()

    assert asyncio.run(due()) == 1
    assert calls[0][0] == "https://example.test/hook"
    assert b"chan_general" in calls[0][1]
    assert client.get(f"/workflows/{workflow_id}/runs").json()[0]["status"] == "succeeded"


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
        live = ws.receive_json()

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
        automated = ws.receive_json()
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
        automated = ws.receive_json()
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
        automated = ws.receive_json()
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
        live = ws.receive_json()

    assert response.json()["status"] == "succeeded"
    assert response.json()["trigger_type"] == "manual"
    assert live["body"] == "Manual run by user_bilal"
    runs = client.get(f'/workflows/{workflow["id"]}/runs').json()
    assert runs[0]["id"] == response.json()["id"]
    assert runs[0]["event"]["initiated_by"] == "user_bilal"


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
        assert ws.receive_json()["body"] == "Edited works now"


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
