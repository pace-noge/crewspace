import datetime as dt

import pytest

from crewspace.application.mcp_connections import normalize_mcp_namespace
from crewspace.domain.entities import McpConnection, McpDiscoveredTool


def test_mcp_postgresql_metadata_enforces_connection_and_approval_states():
    from sqlalchemy.dialects import postgresql
    from sqlalchemy.schema import CreateTable

    from crewspace.infrastructure.models import (
        McpConnectionModel,
        McpDiscoveredToolModel,
    )

    dialect = postgresql.dialect()
    connection_sql = str(
        CreateTable(McpConnectionModel.__table__).compile(dialect=dialect)
    )
    tool_sql = str(
        CreateTable(McpDiscoveredToolModel.__table__).compile(dialect=dialect)
    )
    assert "ck_mcp_connection_enabled" in connection_sql
    assert "ck_mcp_connection_transport" in connection_sql
    assert "uq_mcp_connection_namespace" in connection_sql
    assert "ck_mcp_discovered_tool_approval_state" in tool_sql


def test_mcp_secret_references_are_scoped_and_resolved_write_only(monkeypatch):
    from crewspace.application.mcp_connections import resolve_mcp_secret_ref

    monkeypatch.setenv("CREWSPACE_MCP_JIRA_TOKEN", "resolved-secret-value")
    assert (
        resolve_mcp_secret_ref("env:CREWSPACE_MCP_JIRA_TOKEN")
        == "resolved-secret-value"
    )
    with pytest.raises(ValueError):
        resolve_mcp_secret_ref("resolved-secret-value")
    with pytest.raises(ValueError):
        resolve_mcp_secret_ref("env:PATH")
    with pytest.raises(KeyError):
        resolve_mcp_secret_ref("env:CREWSPACE_MCP_MISSING_TOKEN")


def test_mcp_namespaces_are_stable_and_collision_safe():
    assert normalize_mcp_namespace(" Jira Cloud ") == "jira_cloud"
    assert normalize_mcp_namespace("gitlab-prod") == "gitlab_prod"
    with pytest.raises(ValueError):
        normalize_mcp_namespace("crewspace")
    with pytest.raises(ValueError):
        normalize_mcp_namespace("...")


async def test_mcp_connection_service_rejects_raw_secret_values(app):
    now = dt.datetime.now(dt.timezone.utc)
    connection = McpConnection(
        id="mcp_unsafe",
        name="Unsafe",
        namespace="unsafe",
        transport="streamable_http",
        endpoint_or_command="https://mcp.example.test/api",
        auth_secret_ref="raw-secret-value",
        created_by="user_bilal",
        created_at=now,
        updated_at=now,
    )
    from crewspace.application.mcp_connections import McpConnectionService

    with pytest.raises(ValueError):
        async with app.state.db.uow() as uow:
            await McpConnectionService().create(connection, uow)


async def test_mcp_connection_repository_round_trip_defaults_disabled(app):
    now = dt.datetime.now(dt.timezone.utc)
    connection = McpConnection(
        id="mcp_jira",
        name="Jira",
        namespace="jira",
        transport="streamable_http",
        endpoint_or_command="https://mcp.example.test/api",
        auth_secret_ref="env:CREWSPACE_MCP_JIRA_TOKEN",
        created_by="user_bilal",
        created_at=now,
        updated_at=now,
    )

    async with app.state.db.uow() as uow:
        await uow.mcp_connections.create(connection)
        stored = await uow.mcp_connections.get(connection.id)

    assert stored is not None
    assert stored.namespace == "jira"
    assert stored.enabled is False
    assert stored.auth_secret_ref == "env:CREWSPACE_MCP_JIRA_TOKEN"
    assert "token-value" not in stored.auth_secret_ref


async def test_mcp_connection_namespaces_are_unique(app):
    now = dt.datetime.now(dt.timezone.utc)

    def connection(connection_id: str) -> McpConnection:
        return McpConnection(
            id=connection_id,
            name=connection_id,
            namespace="jira",
            transport="streamable_http",
            endpoint_or_command="https://mcp.example.test/api",
            auth_secret_ref=None,
            created_by="user_bilal",
            created_at=now,
            updated_at=now,
        )

    with pytest.raises(Exception):
        async with app.state.db.uow() as uow:
            await uow.mcp_connections.create(connection("mcp_one"))
            await uow.mcp_connections.create(connection("mcp_two"))


async def test_new_discovered_tool_cannot_self_approve(app):
    now = dt.datetime.now(dt.timezone.utc)
    connection = McpConnection(
        id="mcp_untrusted",
        name="Untrusted",
        namespace="untrusted",
        transport="streamable_http",
        endpoint_or_command="https://mcp.example.test/api",
        auth_secret_ref=None,
        created_by="user_bilal",
        created_at=now,
        updated_at=now,
    )
    tool = McpDiscoveredTool(
        connection_id=connection.id,
        tool_name="dangerous",
        description="Claims approval",
        input_schema={"type": "object", "properties": {}},
        schema_hash="sha256:dangerous",
        approval_state="approved",
        discovered_at=now,
    )
    async with app.state.db.uow() as uow:
        await uow.mcp_connections.create(connection)
        await uow.mcp_connections.upsert_discovered_tool(tool)
        stored = (await uow.mcp_connections.list_discovered_tools(connection.id))[0]
    assert stored.approval_state == "pending"


async def test_discovery_preserves_approval_only_for_unchanged_schema(app):
    now = dt.datetime.now(dt.timezone.utc)
    connection = McpConnection(
        id="mcp_linear",
        name="Linear",
        namespace="linear",
        transport="streamable_http",
        endpoint_or_command="https://mcp.example.test/api",
        auth_secret_ref=None,
        created_by="user_bilal",
        created_at=now,
        updated_at=now,
    )
    approved = McpDiscoveredTool(
        connection_id=connection.id,
        tool_name="create_issue",
        description="Create issue",
        input_schema={"type": "object", "properties": {}},
        schema_hash="sha256:v1",
        approval_state="approved",
        discovered_at=now,
    )
    unchanged = McpDiscoveredTool(
        connection_id=connection.id,
        tool_name=approved.tool_name,
        description=approved.description,
        input_schema=approved.input_schema,
        schema_hash=approved.schema_hash,
        discovered_at=now,
    )
    changed = McpDiscoveredTool(
        connection_id=connection.id,
        tool_name=approved.tool_name,
        description=approved.description,
        input_schema={"type": "object", "properties": {"title": {"type": "string"}}},
        # A provider may replay the old fingerprint; persistence must derive its own.
        schema_hash="sha256:v1",
        discovered_at=now,
    )

    async with app.state.db.uow() as uow:
        await uow.mcp_connections.create(connection)
        await uow.mcp_connections.upsert_discovered_tool(approved)
        await uow.mcp_connections.set_tool_approval_state(
            connection.id, approved.tool_name, "approved"
        )
        await uow.mcp_connections.upsert_discovered_tool(unchanged)
        same = (await uow.mcp_connections.list_discovered_tools(connection.id))[0]
        await uow.mcp_connections.upsert_discovered_tool(changed)
        modified = (await uow.mcp_connections.list_discovered_tools(connection.id))[0]

    assert same.approval_state == "approved"
    assert modified.approval_state == "changed"
    assert modified.schema_hash.startswith("sha256:")
    assert modified.schema_hash != same.schema_hash


async def test_discovered_mcp_tools_default_pending_and_round_trip_schema(app):
    now = dt.datetime.now(dt.timezone.utc)
    connection = McpConnection(
        id="mcp_gitlab",
        name="GitLab",
        namespace="gitlab",
        transport="streamable_http",
        endpoint_or_command="https://mcp.example.test/api",
        auth_secret_ref=None,
        created_by="user_bilal",
        created_at=now,
        updated_at=now,
    )
    tool = McpDiscoveredTool(
        connection_id=connection.id,
        tool_name="create_issue",
        description="Create an issue",
        input_schema={
            "type": "object",
            "properties": {"title": {"type": "string"}},
            "required": ["title"],
        },
        schema_hash="sha256:test",
        discovered_at=now,
    )

    async with app.state.db.uow() as uow:
        await uow.mcp_connections.create(connection)
        await uow.mcp_connections.upsert_discovered_tool(tool)
        tools = await uow.mcp_connections.list_discovered_tools(connection.id)

    assert len(tools) == 1
    assert tools[0].tool_name == "create_issue"
    assert tools[0].approval_state == "pending"
    assert tools[0].input_schema == tool.input_schema
