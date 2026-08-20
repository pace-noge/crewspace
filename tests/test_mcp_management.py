"""Superadmin MCP connection and approval management UI."""
from __future__ import annotations

import asyncio
import datetime as dt


def test_superadmin_manages_mcp_connections_without_rendering_secrets(client, app):
    management = client.get("/management")
    assert 'href="/management/mcp"' in management.text
    assert ">MCP connections<" in management.text

    index = client.get("/management/mcp")
    assert index.status_code == 200
    assert 'class="sidebar"' in index.text
    assert "MCP connections" in index.text
    assert 'href="/management/mcp/new"' in index.text

    create_page = client.get("/management/mcp/new")
    assert create_page.status_code == 200
    assert 'action="/management/mcp"' in create_page.text
    assert "Secret environment reference" in create_page.text

    created = client.post(
        "/management/mcp",
        data={
            "name": "Jira Cloud",
            "namespace": "jira",
            "endpoint": "https://mcp.example.com/api",
            "auth_secret_ref": "env:CREWSPACE_MCP_JIRA_TOKEN",
        },
        follow_redirects=False,
    )
    assert created.status_code == 303
    detail_url = created.headers["location"]
    assert detail_url.startswith("/management/mcp/")

    duplicate = client.post(
        "/management/mcp",
        data={
            "name": "Duplicate Jira",
            "namespace": "jira",
            "endpoint": "https://other.example.com/api",
            "auth_secret_ref": "",
        },
    )
    assert duplicate.status_code == 422

    detail = client.get(detail_url)
    assert detail.status_code == 200
    assert "Jira Cloud" in detail.text
    assert "jira" in detail.text
    assert "Disabled" in detail.text
    assert "Secret reference configured" in detail.text
    assert "CREWSPACE_MCP_JIRA_TOKEN" not in detail.text

    async def missing_secret_factory(_connection):
        raise KeyError("MCP secret environment variable 'CREWSPACE_MCP_JIRA_TOKEN' is not set")

    from crewspace.api.routers import teams

    original_factory = teams.build_external_discovery_client
    teams.build_external_discovery_client = missing_secret_factory
    try:
        failed_discovery = client.post(f"{detail_url}/discover")
    finally:
        teams.build_external_discovery_client = original_factory
    assert failed_discovery.status_code == 422
    assert "CREWSPACE_MCP_JIRA_TOKEN" not in failed_discovery.text

    connection_id = detail_url.rsplit("/", 1)[-1]

    async def seed_tool():
        from crewspace.domain.entities import McpDiscoveredTool

        async with app.state.db.uow() as uow:
            await uow.mcp_connections.upsert_discovered_tool(
                McpDiscoveredTool(
                    connection_id=connection_id,
                    tool_name="create_issue",
                    description="Create an issue",
                    input_schema={"type": "object"},
                    schema_hash="ignored",
                    discovered_at=dt.datetime.now(dt.timezone.utc),
                )
            )
            await uow.commit()

    asyncio.run(seed_tool())

    detail = client.get(detail_url)
    assert "jira.create_issue" in detail.text
    assert "Pending" in detail.text
    assert f'action="{detail_url}/tools/create_issue/approval"' in detail.text

    approved = client.post(
        f"{detail_url}/tools/create_issue/approval",
        data={"state": "approved"},
        follow_redirects=False,
    )
    assert approved.status_code == 303
    assert approved.headers["location"] == detail_url
    assert "Approved" in client.get(detail_url).text


def test_concurrent_mcp_namespace_conflict_returns_validation_error(client, monkeypatch):
    from sqlalchemy.exc import IntegrityError

    from crewspace.infrastructure.sql import SqlAlchemyConnection

    original_execute = SqlAlchemyConnection.execute

    async def concurrent_loser(connection, sql, params=()):
        if sql.startswith("INSERT INTO mcp_connection"):
            raise IntegrityError(
                sql,
                params or {},
                Exception("UNIQUE constraint failed: mcp_connection.namespace"),
            )
        return await original_execute(connection, sql, params)

    monkeypatch.setattr(SqlAlchemyConnection, "execute", concurrent_loser)
    response = client.post(
        "/management/mcp",
        data={
            "name": "Racing Jira",
            "namespace": "jira",
            "endpoint": "https://mcp.example.com/api",
            "auth_secret_ref": "",
        },
    )

    assert response.status_code == 422
    assert response.json() == {"detail": "MCP namespace is already in use"}


def test_mcp_create_does_not_mask_unrelated_integrity_errors(client, monkeypatch):
    import pytest
    from sqlalchemy.exc import IntegrityError

    from crewspace.infrastructure.sql import SqlAlchemyConnection

    original_execute = SqlAlchemyConnection.execute

    async def foreign_key_failure(connection, sql, params=()):
        if sql.startswith("INSERT INTO mcp_connection"):
            raise IntegrityError(sql, params, Exception("FOREIGN KEY constraint failed"))
        return await original_execute(connection, sql, params)

    monkeypatch.setattr(SqlAlchemyConnection, "execute", foreign_key_failure)
    with pytest.raises(IntegrityError, match="FOREIGN KEY constraint failed"):
        client.post(
            "/management/mcp",
            data={
                "name": "Broken FK",
                "namespace": "broken_fk",
                "endpoint": "https://mcp.example.com/api",
                "auth_secret_ref": "",
            },
        )


def test_superadmin_enables_and_rediscovers_mcp_connection(client, monkeypatch):
    created = client.post(
        "/management/mcp",
        data={
            "name": "Issue Tracker",
            "namespace": "issues",
            "endpoint": "https://mcp.example.com/api",
            "auth_secret_ref": "",
        },
        follow_redirects=False,
    )
    detail_url = created.headers["location"]

    class FakeDiscoveryClient:
        async def list_tools(self):
            from crewspace.application.mcp_catalog import DiscoveredMcpTool

            return [
                DiscoveredMcpTool(
                    "create_issue",
                    "Create an issue",
                    {"type": "object", "properties": {"title": {"type": "string"}}},
                )
            ]

    async def fake_factory(_connection):
        return FakeDiscoveryClient()

    monkeypatch.setattr(
        "crewspace.api.routers.teams.build_external_discovery_client",
        fake_factory,
    )

    discovered = client.post(f"{detail_url}/discover", follow_redirects=False)
    assert discovered.status_code == 303
    assert discovered.headers["location"] == detail_url
    page = client.get(detail_url)
    assert "issues.create_issue" in page.text
    assert "Pending" in page.text

    enabled = client.post(
        f"{detail_url}/enabled", data={"enabled": "true"}, follow_redirects=False
    )
    assert enabled.status_code == 303
    assert "Enabled" in client.get(detail_url).text

    disabled = client.post(
        f"{detail_url}/enabled", data={"enabled": "false"}, follow_redirects=False
    )
    assert disabled.status_code == 303
    assert "Disabled" in client.get(detail_url).text


def test_non_superadmin_cannot_access_or_mutate_mcp_management(client, app):
    async def demote():
        async with app.state.db.uow() as uow:
            await uow._conn.execute(
                "UPDATE member SET role='team_member' WHERE id='user_bilal'"
            )
            await uow.commit()

    asyncio.run(demote())

    assert client.get("/management/mcp").status_code == 403
    assert client.get("/management/mcp/new").status_code == 403
    assert client.post(
        "/management/mcp",
        data={
            "name": "Unsafe",
            "namespace": "unsafe",
            "endpoint": "https://mcp.example.com/api",
            "auth_secret_ref": "",
        },
    ).status_code == 403
    assert client.post("/management/mcp/unknown/discover").status_code == 403
    assert client.post(
        "/management/mcp/unknown/enabled", data={"enabled": "true"}
    ).status_code == 403
    assert client.post(
        "/management/mcp/unknown/tools/tool/approval", data={"state": "approved"}
    ).status_code == 403
