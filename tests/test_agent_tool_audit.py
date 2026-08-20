import datetime as dt

from crewspace.domain.entities import AgentToolCall


async def test_agent_tool_audit_repository_round_trip(app):
    now = dt.datetime.now(dt.timezone.utc)
    call = AgentToolCall(
        id="atc_roundtrip",
        agent_id="agent_crewspace",
        initiator_id="user_bilal",
        provider_type="native",
        provider_id="crewspace",
        tool_name="post_message",
        status="allowed",
        arguments_redacted='{"body":"hello"}',
        created_at=now,
    )

    async with app.state.db.uow() as uow:
        await uow.agent_tool_calls.create(call)
        await uow.agent_tool_calls.finish(
            call.id,
            status="succeeded",
            duration_ms=12,
            result_summary='{"id":"msg_1"}',
            error=None,
        )
        rows = await uow.agent_tool_calls.list_recent(limit=10)

    stored = next(row for row in rows if row.id == call.id)
    assert stored.agent_id == "agent_crewspace"
    assert stored.initiator_id == "user_bilal"
    assert stored.status == "succeeded"
    assert stored.duration_ms == 12
    assert stored.arguments_redacted == '{"body":"hello"}'
    assert stored.result_summary == '{"id":"msg_1"}'
    assert stored.error is None


async def test_blocked_tool_attempt_survives_request_rollback(app):
    from crewspace.application.tools import ToolPermissionDenied, build_registry

    try:
        async with app.state.db.uow() as uow:
            runner = build_registry().bind(
                uow,
                principal_id="user_bilal",
                agent_id="agent_crewspace",
                allowed_tools=set(),
            )
            await runner.run("list_boards")
    except ToolPermissionDenied:
        pass

    async with app.state.db.uow() as uow:
        rows = await uow.agent_tool_calls.list_recent(limit=10)
    blocked = next(row for row in rows if row.tool_name == "list_boards")
    assert blocked.status == "blocked"
    assert blocked.agent_id == "agent_crewspace"
    assert blocked.initiator_id == "user_bilal"


async def test_successful_tool_call_is_redacted_and_bounded(app):
    from crewspace.application.tools import Tool, ToolRegistry

    async def reveal(uow, principal_id, actor_id, token, payload):
        return {"token": token, "payload": payload}

    registry = ToolRegistry()
    registry.register(
        Tool(
            "reveal",
            "Test redaction",
            {
                "type": "object",
                "properties": {
                    "token": {"type": "string"},
                    "payload": {"type": "string"},
                },
                "required": ["token", "payload"],
            },
            reveal,
        )
    )
    secret = "do-not-persist-this-secret"
    async with app.state.db.uow() as uow:
        runner = registry.bind(
            uow,
            principal_id="user_bilal",
            agent_id="agent_crewspace",
            allowed_tools={"reveal"},
        )
        await runner.run("reveal", token=secret, payload="x" * 5000)

    async with app.state.db.uow() as uow:
        rows = await uow.agent_tool_calls.list_recent(limit=10)
    stored = next(row for row in rows if row.tool_name == "reveal")
    assert stored.status == "succeeded"
    assert secret not in stored.arguments_redacted
    assert secret not in (stored.result_summary or "")
    assert "[REDACTED]" in stored.arguments_redacted
    assert len(stored.arguments_redacted) <= 2048
    assert len(stored.result_summary or "") <= 2048


async def test_agent_tool_audit_prunes_oldest_rows(app):
    now = dt.datetime.now(dt.timezone.utc)
    async with app.state.db.uow() as uow:
        for index in range(5):
            await uow.agent_tool_calls.create(
                AgentToolCall(
                    id=f"atc_prune_{index}",
                    agent_id="agent_crewspace",
                    initiator_id="user_bilal",
                    provider_type="native",
                    provider_id="crewspace",
                    tool_name="list_boards",
                    status="succeeded",
                    arguments_redacted="{}",
                    created_at=now + dt.timedelta(seconds=index),
                )
            )
        await uow.agent_tool_calls.prune(keep=3)

    async with app.state.db.uow() as uow:
        rows = await uow.agent_tool_calls.list_recent(limit=10)
    ids = {row.id for row in rows if row.id.startswith("atc_prune_")}
    assert ids == {"atc_prune_2", "atc_prune_3", "atc_prune_4"}


def test_audit_redactor_covers_common_embedded_secret_formats():
    from crewspace.application.tools import _bounded_audit_text

    secrets = {
        "bearer": "Bearer bearer-secret-value",
        "url": "https://mcp.test/tools?access_token=url-secret-value&x=1",
        "credential": "credential=credential-secret-value",
        "cookie": "cookie: cookie-secret-value",
        "ssh": "ssh_key=ssh-secret-value",
        "json": '{"password":"json-secret-value","safe":"visible"}',
        "pem": "-----BEGIN PRIVATE KEY-----\npem-secret-value\n-----END PRIVATE KEY-----",
        "basic": "Authorization: Basic basic-secret-value",
        "spaced": "password=my spaced secret value",
        "cookies": "Cookie: session=cookie-session-secret; csrftoken=cookie-csrf-secret",
    }
    rendered = _bounded_audit_text(secrets)
    for secret in (
        "bearer-secret-value",
        "url-secret-value",
        "credential-secret-value",
        "cookie-secret-value",
        "ssh-secret-value",
        "json-secret-value",
        "pem-secret-value",
        "basic-secret-value",
        "my spaced secret value",
        "cookie-session-secret",
        "cookie-csrf-secret",
    ):
        assert secret not in rendered
    assert "[REDACTED]" in rendered
    assert "visible" in rendered


async def test_cancelled_tool_attempt_records_terminal_failure(app):
    import asyncio

    from crewspace.application.tools import Tool, ToolRegistry

    async def cancel(uow, principal_id, actor_id):
        raise asyncio.CancelledError()

    registry = ToolRegistry()
    registry.register(
        Tool(
            "cancelled_tool",
            "Test cancellation audit",
            {"type": "object", "properties": {}},
            cancel,
        )
    )
    try:
        async with app.state.db.uow() as uow:
            await registry.bind(
                uow,
                principal_id="user_bilal",
                agent_id="agent_crewspace",
                allowed_tools={"cancelled_tool"},
            ).run("cancelled_tool")
    except asyncio.CancelledError:
        pass

    async with app.state.db.uow() as uow:
        rows = await uow.agent_tool_calls.list_recent(limit=20)
    cancelled = next(row for row in rows if row.tool_name == "cancelled_tool")
    assert cancelled.status == "failed"
    assert "CancelledError" in (cancelled.error or "")


async def test_failed_tool_attempt_survives_request_rollback(app):
    from crewspace.application.tools import Tool, ToolRegistry

    async def explode(uow, principal_id, actor_id):
        raise RuntimeError("credential token=never-store-this")

    registry = ToolRegistry()
    registry.register(
        Tool(
            "explode",
            "Test failure audit",
            {"type": "object", "properties": {}},
            explode,
        )
    )
    try:
        async with app.state.db.uow() as uow:
            runner = registry.bind(
                uow,
                principal_id="user_bilal",
                agent_id="agent_crewspace",
                allowed_tools={"explode"},
            )
            await runner.run("explode")
    except RuntimeError:
        pass

    async with app.state.db.uow() as uow:
        rows = await uow.agent_tool_calls.list_recent(limit=10)
    failed = next(row for row in rows if row.tool_name == "explode")
    assert failed.status == "failed"
    assert "never-store-this" not in (failed.error or "")


def test_superadmin_can_view_redacted_tool_history(client, app):
    import asyncio

    from crewspace.application.tools import Tool, ToolRegistry

    secret = "history-must-not-render-this"

    async def generate():
        async def reveal(uow, principal_id, actor_id, password):
            return {"ok": True, "password": password}

        registry = ToolRegistry()
        registry.register(
            Tool(
                "history_test",
                "History test",
                {
                    "type": "object",
                    "properties": {"password": {"type": "string"}},
                    "required": ["password"],
                },
                reveal,
            )
        )
        async with app.state.db.uow() as uow:
            await registry.bind(
                uow,
                principal_id="user_bilal",
                agent_id="agent_crewspace",
                allowed_tools={"history_test"},
            ).run("history_test", password=secret)

    asyncio.run(generate())
    response = client.get("/management/tools/runs")
    assert response.status_code == 200
    assert "Tool execution history" in response.text
    assert "history_test" in response.text
    assert "Succeeded" in response.text
    assert "[REDACTED]" in response.text
    assert secret not in response.text


def test_non_superadmin_cannot_view_tool_history(client, app):
    import asyncio

    async def demote():
        async with app.state.db.uow() as uow:
            await uow._conn.execute(
                "UPDATE member SET role='team_member' WHERE id='user_bilal'"
            )

    asyncio.run(demote())
    assert client.get("/management/tools/runs").status_code == 403


async def test_trusted_remote_agent_execution_records_agent_identity(app):
    from crewspace.application.tools import build_registry

    async with app.state.db.uow() as uow:
        await build_registry().bind_trusted(
            uow,
            principal_id="agent_planner",
            agent_id="agent_planner",
        ).run("list_boards")

    async with app.state.db.uow() as uow:
        rows = await uow.agent_tool_calls.list_recent(limit=20)
    stored = next(row for row in rows if row.tool_name == "list_boards")
    assert stored.agent_id == "agent_planner"
    assert stored.initiator_id == "agent_planner"


async def test_tool_audit_survives_actor_deletion(app):
    call = AgentToolCall(
        id="atc_deleted_actor",
        agent_id="agent_departed",
        initiator_id=None,
        provider_type="native",
        provider_id="crewspace",
        tool_name="list_boards",
        status="succeeded",
        arguments_redacted="{}",
        created_at=dt.datetime.now(dt.timezone.utc),
    )
    async with app.state.db.uow() as uow:
        await uow._conn.execute(
            "INSERT INTO member (id, kind, name, role, backend, uses_app_llm) "
            "VALUES ('agent_departed', 'agent', 'Departed', 'agent', 'stub', 0)"
        )
        await uow.agent_tool_calls.create(call)
        await uow._conn.execute("DELETE FROM member WHERE id='agent_departed'")

    async with app.state.db.uow() as uow:
        rows = await uow.agent_tool_calls.list_recent(limit=20)
    assert any(row.id == call.id and row.agent_id == "agent_departed" for row in rows)


def test_agent_tool_audit_table_is_part_of_postgresql_metadata():
    from sqlalchemy.dialects import postgresql
    from sqlalchemy.schema import CreateTable

    from crewspace.infrastructure.models import Base

    table = Base.metadata.tables["agent_tool_call"]
    sql = str(CreateTable(table).compile(dialect=postgresql.dialect()))
    assert "agent_tool_call" in sql
    assert "ck_agent_tool_call_status" in sql
    assert "FOREIGN KEY(agent_id)" not in sql
    assert "FOREIGN KEY(initiator_id)" not in sql
