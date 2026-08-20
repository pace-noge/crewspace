"""Namespaced MCP permissions and audited execution."""
from __future__ import annotations

import datetime as dt

from crewspace.domain.entities import McpConnection, McpDiscoveredTool


async def _seed_approved_tool(app, *, enabled: bool = True):
    now = dt.datetime.now(dt.timezone.utc)
    connection = McpConnection(
        id="mcp_jira",
        name="Jira",
        namespace="jira",
        transport="streamable_http",
        endpoint_or_command="https://mcp.example.com/api",
        auth_secret_ref=None,
        created_by="user_bilal",
        created_at=now,
        updated_at=now,
    )
    async with app.state.db.uow() as uow:
        await uow.mcp_connections.create(connection)
        await uow.mcp_connections.upsert_discovered_tool(
            McpDiscoveredTool(
                connection_id=connection.id,
                tool_name="create_issue",
                description="Create a Jira issue",
                input_schema={
                    "type": "object",
                    "properties": {"title": {"type": "string"}},
                    "required": ["title"],
                },
                schema_hash="ignored",
                discovered_at=now,
            )
        )
        await uow.mcp_connections.set_tool_approval_state(
            connection.id, "create_issue", "approved"
        )
        await uow.mcp_connections.set_enabled(connection.id, enabled)
        await uow.commit()
    return connection


async def test_mcp_tool_requires_effective_grant_and_records_provenance(app):
    from crewspace.application.mcp_tools import build_agent_tool_runtime
    from crewspace.application.tools import ToolPermissionDenied, build_registry

    connection = await _seed_approved_tool(app)
    executions = []

    class Executor:
        async def call_tool(self, active_connection, tool_name, arguments):
            executions.append((active_connection.id, tool_name, arguments))
            return {"issue_key": "ENG-42", "title": arguments["title"]}

    async with app.state.db.uow() as uow:
        denied = await build_agent_tool_runtime(
            build_registry(), uow,
            principal_id="user_bilal",
            agent_id="agent_crewspace",
            executor=Executor(),
        )
        assert "jira.create_issue" not in {tool.name for tool in denied.tools}
        try:
            await denied.runner.run("jira.create_issue", title="Denied")
        except ToolPermissionDenied:
            pass
        else:
            raise AssertionError("ungranted MCP tool must not execute")

    async with app.state.db.uow() as uow:
        await uow.agent_policies.replace_mcp_tools(
            "agent_crewspace", {(connection.id, "create_issue")}
        )
        runtime = await build_agent_tool_runtime(
            build_registry(), uow,
            principal_id="user_bilal",
            agent_id="agent_crewspace",
            executor=Executor(),
        )
        assert {tool.name for tool in runtime.tools if tool.provider == "mcp_jira"} == {
            "jira.create_issue"
        }
        result = await runtime.runner.run("jira.create_issue", title="Ship MCP")
        assert result == {"issue_key": "ENG-42", "title": "Ship MCP"}

    assert executions == [("mcp_jira", "create_issue", {"title": "Ship MCP"})]

    async with app.state.db.uow() as uow:
        calls = await uow.agent_tool_calls.list_recent(limit=20)
    succeeded = next(
        call for call in calls
        if call.tool_name == "jira.create_issue" and call.status == "succeeded"
    )
    assert succeeded.provider_type == "mcp"
    assert succeeded.provider_id == "mcp_jira"
    assert succeeded.agent_id == "agent_crewspace"
    assert succeeded.initiator_id == "user_bilal"


def test_superadmin_assigns_only_active_approved_mcp_tools(client, app):
    import asyncio

    connection = asyncio.run(_seed_approved_tool(app))

    page = client.get("/management/agents/agent_crewspace/settings")
    assert page.status_code == 200
    assert "External MCP" in page.text
    assert 'name="mcp_tool_names" value="jira.create_issue"' in page.text
    assert "Create a Jira issue" in page.text

    updated = client.post(
        "/management/agents/agent_crewspace/tools",
        data={
            "tool_names": ["list_boards"],
            "mcp_tool_names": ["jira.create_issue"],
        },
        follow_redirects=False,
    )
    assert updated.status_code == 303

    async def grants():
        async with app.state.db.uow() as uow:
            return await uow.agent_policies.list_enabled_mcp_tools(
                "agent_crewspace"
            )

    assert asyncio.run(grants()) == {(connection.id, "create_issue")}

    rejected = client.post(
        "/management/agents/agent_crewspace/tools",
        data={"mcp_tool_names": ["jira.not_discovered"]},
    )
    assert rejected.status_code == 422
    assert asyncio.run(grants()) == {(connection.id, "create_issue")}


async def test_agent_registry_exposes_granted_mcp_schema_to_builtin_llm(app):
    from crewspace.config import Settings
    from crewspace.infrastructure.agents.registry import AgentRegistry

    connection = await _seed_approved_tool(app)
    async with app.state.db.uow() as uow:
        await uow.agent_policies.replace_native_tools("agent_crewspace", set())
        await uow.agent_policies.replace_mcp_tools(
            "agent_crewspace", {(connection.id, "create_issue")}
        )
        no_principal = await AgentRegistry.build(Settings(), uow)
        assert no_principal._local["agent_crewspace"]._tools == []
        provider = await AgentRegistry.build(
            Settings(), uow, principal_id="user_bilal"
        )
        crewspace = provider._local["agent_crewspace"]

    assert {tool.name for tool in crewspace._tools} == {"jira.create_issue"}
    external = crewspace._tools[0]
    assert external.input_schema["required"] == ["title"]


async def test_chat_service_routes_namespaced_mcp_call_through_composite_runner(app, monkeypatch):
    from crewspace.application.services import ChatService
    from crewspace.application.tools import build_registry
    from crewspace.config import Settings
    from crewspace.infrastructure.agents import registry as agent_registry


    connection = await _seed_approved_tool(app)
    executions = []

    class Executor:
        async def call_tool(self, active_connection, tool_name, arguments):
            executions.append((active_connection.id, tool_name, arguments))
            return {"issue_key": "ENG-43"}

    class Provider:
        def resolve(self, text):
            return "agent_crewspace"

        async def on_chat_message(self, text, runner, context=None):
            result = await runner.run("jira.create_issue", title="From chat")
            return "agent_crewspace", [result["issue_key"]]

    async def build_provider(settings, uow, *, principal_id=None):
        assert principal_id == "user_bilal"
        return Provider()

    async def build_executor(**kwargs):
        return Executor()

    monkeypatch.setattr(agent_registry.AgentRegistry, "build", build_provider)


    async with app.state.db.uow() as uow:
        await uow.agent_policies.replace_mcp_tools(
            "agent_crewspace", {(connection.id, "create_issue")}
        )
        messages = await ChatService(
            build_registry(), Settings(), mcp_executor_factory=build_executor,
        ).post_and_respond(
            "channel_general", "user_bilal", "@crewspace create issue", uow
        )

    assert executions == [("mcp_jira", "create_issue", {"title": "From chat"})]
    assert messages[-1].body == "ENG-43"


def test_chat_application_service_does_not_import_mcp_infrastructure():
    import inspect

    from crewspace.application import services

    source = inspect.getsource(services)
    assert "infrastructure import mcp_client" not in source
    assert "infrastructure.mcp_client" not in source


async def test_mcp_execution_requires_principal_and_valid_bounded_arguments(app):
    import pytest

    from crewspace.application.mcp_tools import build_agent_tool_runtime
    from crewspace.application.tools import ToolPermissionDenied, build_registry

    connection = await _seed_approved_tool(app)

    class Executor:
        async def call_tool(self, active_connection, tool_name, arguments):
            raise AssertionError("invalid MCP call must not reach the provider")

    async with app.state.db.uow() as uow:
        await uow.agent_policies.replace_mcp_tools(
            "agent_crewspace", {(connection.id, "create_issue")}
        )
        no_principal = await build_agent_tool_runtime(
            build_registry(), uow,
            principal_id=None,
            agent_id="agent_crewspace",
            executor=Executor(),
        )
        with pytest.raises(ToolPermissionDenied):
            await no_principal.runner.run("jira.create_issue", title="No principal")

    async with app.state.db.uow() as uow:
        runtime = await build_agent_tool_runtime(
            build_registry(), uow,
            principal_id="user_bilal",
            agent_id="agent_crewspace",
            executor=Executor(),
        )
        with pytest.raises(ValueError, match="arguments"):
            await runtime.runner.run("jira.create_issue")
        with pytest.raises(ValueError, match="too large"):
            await runtime.runner.run("jira.create_issue", title="x" * 70_000)


async def test_mcp_execution_errors_do_not_expose_secret_reference(app):
    import pytest

    from crewspace.application.mcp_tools import build_agent_tool_runtime
    from crewspace.application.tools import build_registry

    connection = await _seed_approved_tool(app)
    connection.auth_secret_ref = "env:CREWSPACE_MCP_JIRA_TOKEN"
    async with app.state.db.uow() as uow:
        await uow._conn.execute(
            "UPDATE mcp_connection SET auth_secret_ref=? WHERE id=?",
            (connection.auth_secret_ref, connection.id),
        )
        await uow.agent_policies.replace_mcp_tools(
            "agent_crewspace", {(connection.id, "create_issue")}
        )

        class Executor:
            async def call_tool(self, active_connection, tool_name, arguments):
                raise KeyError(
                    "MCP secret environment variable 'CREWSPACE_MCP_JIRA_TOKEN' is not set"
                )

        runtime = await build_agent_tool_runtime(
            build_registry(), uow,
            principal_id="user_bilal",
            agent_id="agent_crewspace",
            executor=Executor(),
        )
        with pytest.raises(RuntimeError, match="External MCP tool execution failed") as caught:
            await runtime.runner.run("jira.create_issue", title="No leak")
        assert "CREWSPACE_MCP_JIRA_TOKEN" not in str(caught.value)

    async with app.state.db.uow() as uow:
        calls = await uow.agent_tool_calls.list_recent(limit=20)
    failed = next(call for call in calls if call.tool_name == "jira.create_issue")
    assert "CREWSPACE_MCP_JIRA_TOKEN" not in (failed.error or "")


async def test_mcp_runner_rechecks_connection_and_approval_at_execution(app):
    from crewspace.application.mcp_tools import build_agent_tool_runtime
    from crewspace.application.tools import ToolPermissionDenied, build_registry

    connection = await _seed_approved_tool(app)

    class Executor:
        async def call_tool(self, active_connection, tool_name, arguments):
            raise AssertionError("revoked MCP tool must not reach the provider")

    async with app.state.db.uow() as uow:
        await uow.agent_policies.replace_mcp_tools(
            "agent_crewspace", {(connection.id, "create_issue")}
        )
        runtime = await build_agent_tool_runtime(
            build_registry(), uow,
            principal_id="user_bilal",
            agent_id="agent_crewspace",
            executor=Executor(),
        )
        await uow.mcp_connections.set_tool_approval_state(
            connection.id, "create_issue", "changed"
        )
        try:
            await runtime.runner.run("jira.create_issue", title="Revoked")
        except ToolPermissionDenied:
            pass
        else:
            raise AssertionError("schema-changed tool must require reapproval")
