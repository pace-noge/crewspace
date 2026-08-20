import datetime as dt

from mcp.server import MCPServer

from crewspace.domain.entities import McpConnection


async def test_in_process_mcp_discovery_persists_pending_namespaced_tools(app):
    from crewspace.application.mcp_connections import discover_mcp_tools
    from crewspace.infrastructure.mcp_client import McpDiscoveryClient

    server = MCPServer(name="fake-external", version="1.0")

    async def create_issue(title: str) -> str:
        return title

    server.add_tool(create_issue, name="create_issue", description="Create issue")
    now = dt.datetime.now(dt.timezone.utc)
    connection = McpConnection(
        id="mcp_jira_discovery",
        name="Jira",
        namespace="jira",
        transport="streamable_http",
        endpoint_or_command="https://mcp.example.test/api",
        auth_secret_ref=None,
        created_by="user_bilal",
        created_at=now,
        updated_at=now,
    )

    async with app.state.db.uow() as uow:
        await uow.mcp_connections.create(connection)
        discovered = await discover_mcp_tools(
            connection,
            uow,
            client=McpDiscoveryClient(server, timeout_seconds=2, max_tools=10),
        )

    assert [tool.qualified_name for tool in discovered] == ["jira.create_issue"]
    async with app.state.db.uow() as uow:
        stored = await uow.mcp_connections.list_discovered_tools(connection.id)
    assert stored[0].approval_state == "pending"
    assert stored[0].input_schema["type"] == "object"


async def test_mcp_discovery_service_rejects_unvalidated_catalog(app):
    import pytest

    from crewspace.application.mcp_connections import discover_mcp_tools
    from crewspace.infrastructure.mcp_client import DiscoveredMcpTool, McpDiscoveryError

    class UnsafeCatalog:
        async def list_tools(self):
            return [
                DiscoveredMcpTool(
                    name="unsafe.tool",
                    description="Unsafe",
                    input_schema={"type": "object"},
                )
            ]

    now = dt.datetime.now(dt.timezone.utc)
    connection = McpConnection(
        id="mcp_unvalidated",
        name="Unvalidated",
        namespace="unvalidated",
        transport="streamable_http",
        endpoint_or_command="https://mcp.example.test/api",
        auth_secret_ref=None,
        created_by="user_bilal",
        created_at=now,
        updated_at=now,
    )
    with pytest.raises(McpDiscoveryError):
        async with app.state.db.uow() as uow:
            await uow.mcp_connections.create(connection)
            await discover_mcp_tools(connection, uow, client=UnsafeCatalog())


async def test_mcp_discovery_disables_tools_removed_from_catalog(app):
    from crewspace.application.mcp_connections import discover_mcp_tools
    from crewspace.infrastructure.mcp_client import DiscoveredMcpTool

    class Catalog:
        def __init__(self, tools):
            self._tools = tools

        async def list_tools(self):
            return self._tools

    now = dt.datetime.now(dt.timezone.utc)
    connection = McpConnection(
        id="mcp_removed_tools",
        name="Removed tools",
        namespace="removed",
        transport="streamable_http",
        endpoint_or_command="https://mcp.example.test/api",
        auth_secret_ref=None,
        created_by="user_bilal",
        created_at=now,
        updated_at=now,
    )
    first = DiscoveredMcpTool(
        name="first", description="First", input_schema={"type": "object"}
    )
    second = DiscoveredMcpTool(
        name="second", description="Second", input_schema={"type": "object"}
    )
    async with app.state.db.uow() as uow:
        await uow.mcp_connections.create(connection)
        await discover_mcp_tools(connection, uow, client=Catalog([first, second]))
        await uow.mcp_connections.set_tool_approval_state(
            connection.id, "second", "approved"
        )
        await discover_mcp_tools(connection, uow, client=Catalog([first]))
        stored = await uow.mcp_connections.list_discovered_tools(connection.id)

    states = {tool.tool_name: tool.approval_state for tool in stored}
    assert states == {"first": "pending", "second": "disabled"}


async def test_external_mcp_endpoint_validation_blocks_ssrf_targets():
    import pytest

    from crewspace.infrastructure.mcp_client import validate_mcp_endpoint

    async def public(_host: str):
        return {"8.8.8.8"}

    async def private(_host: str):
        return {"10.0.0.5"}

    assert await validate_mcp_endpoint(
        "https://mcp.example.test/api", resolver=public
    ) == "https://mcp.example.test/api"
    for endpoint in (
        "http://mcp.example.test/api",
        "https://user:password@mcp.example.test/api",
        "https://mcp.example.test/api?access_token=raw-secret-value",
        "https://mcp.example.test/api#credential-data",
        "https://localhost/api",
        "https://127.0.0.1/api",
    ):
        with pytest.raises(ValueError):
            await validate_mcp_endpoint(endpoint, resolver=public)
    with pytest.raises(ValueError):
        await validate_mcp_endpoint(
            "https://mcp.example.test/api", resolver=private
        )

    async def translated_private(_host: str):
        return {"64:ff9b::7f00:1"}

    with pytest.raises(ValueError):
        await validate_mcp_endpoint(
            "https://mcp.example.test/api", resolver=translated_private
        )


async def test_external_transport_stops_response_before_byte_limit_is_exceeded():
    import httpx2
    import pytest

    from crewspace.infrastructure.mcp_client import (
        McpResponseTooLarge,
        _LimitedResponseTransport,
    )

    class Chunks(httpx2.AsyncByteStream):
        closed = False

        async def __aiter__(self):
            yield b"1234"
            yield b"5678"

        async def aclose(self):
            self.closed = True

    stream = Chunks()

    class Delegate(httpx2.AsyncBaseTransport):
        async def handle_async_request(self, request):
            return httpx2.Response(200, stream=stream, request=request)

        async def aclose(self):
            return None

    transport = _LimitedResponseTransport(Delegate(), max_response_bytes=6)
    response = await transport.handle_async_request(
        httpx2.Request("POST", "https://mcp.example.test/api")
    )
    with pytest.raises(McpResponseTooLarge):
        await response.aread()
    assert stream.closed is True


async def test_pinned_backend_never_resolves_original_hostname():
    from crewspace.infrastructure.mcp_client import _PinnedNetworkBackend

    calls = []

    class Delegate:
        async def connect_tcp(self, host, port, *args):
            calls.append((host, port))
            return "stream"

    backend = _PinnedNetworkBackend({"8.8.8.8"}, delegate=Delegate())
    assert await backend.connect_tcp("mcp.example.test", 443) == "stream"
    assert calls == [("8.8.8.8", 443)]


async def test_external_mcp_client_factory_uses_secret_reference_and_supported_transport(monkeypatch):
    import pytest

    from crewspace.infrastructure.mcp_client import build_external_discovery_client

    now = dt.datetime.now(dt.timezone.utc)
    connection = McpConnection(
        id="mcp_external",
        name="External",
        namespace="external",
        transport="streamable_http",
        endpoint_or_command="https://mcp.example.test/api",
        auth_secret_ref="env:CREWSPACE_MCP_EXTERNAL_TOKEN",
        created_by="user_bilal",
        created_at=now,
        updated_at=now,
    )
    monkeypatch.setenv("CREWSPACE_MCP_EXTERNAL_TOKEN", "secret-bearer-value")

    async def public(_host: str):
        return {"8.8.8.8"}

    client = await build_external_discovery_client(connection, resolver=public)
    assert client.pinned_addresses == {"8.8.8.8"}
    assert client.authorization_headers == {
        "Authorization": "Bearer secret-bearer-value"
    }
    assert "secret-bearer-value" not in client.endpoint
    assert client.follow_redirects is False

    connection.transport = "sse"
    with pytest.raises(ValueError):
        await build_external_discovery_client(connection, resolver=public)


async def test_mcp_discovery_rejects_duplicate_or_unsafe_tool_names():
    import pytest

    from crewspace.infrastructure.mcp_client import (
        DiscoveredMcpTool,
        McpDiscoveryError,
        validate_discovered_tools,
    )

    schema = {"type": "object"}
    with pytest.raises(McpDiscoveryError):
        validate_discovered_tools(
            [
                DiscoveredMcpTool("same", "One", schema),
                DiscoveredMcpTool("same", "Two", schema),
            ],
            max_tools=10,
            max_catalog_bytes=10_000,
        )
    with pytest.raises(McpDiscoveryError):
        validate_discovered_tools(
            [DiscoveredMcpTool("jira.create_issue", "Unsafe", schema)],
            max_tools=10,
            max_catalog_bytes=10_000,
        )
    deeply_nested: dict[str, object] = {"type": "object"}
    nested: dict[str, object] = deeply_nested
    for _ in range(100):
        child = {}
        nested["properties"] = {"child": child}
        nested = child

    for invalid_schema in (
        {
            "type": "object",
            "$schema": 1,
            "properties": "not-an-object",
        },
        deeply_nested,
        {
            "type": "object",
            "properties": {"item": {"$ref": "https://attacker.invalid/schema"}},
        },
        {
            "type": "object",
            "$dynamicRef": "https://attacker.invalid/schema",
        },
        {
            "type": "object",
            "$recursiveRef": "https://attacker.invalid/schema",
        },
    ):
        with pytest.raises(McpDiscoveryError):
            validate_discovered_tools(
                [DiscoveredMcpTool("invalid_schema", "Invalid", invalid_schema)],
                max_tools=10,
                max_catalog_bytes=10_000,
            )


async def test_mcp_discovery_reads_all_catalog_pages(monkeypatch):
    from types import SimpleNamespace

    from crewspace.infrastructure import mcp_client
    from crewspace.infrastructure.mcp_client import McpDiscoveryClient

    calls = []

    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def list_tools(self, *, cursor=None, cache_mode="use"):
            calls.append((cursor, cache_mode))
            if cursor is None:
                return SimpleNamespace(
                    tools=[SimpleNamespace(
                        name="first", description="First",
                        input_schema={"type": "object"},
                    )],
                    next_cursor="page-2",
                )
            return SimpleNamespace(
                tools=[SimpleNamespace(
                    name="second", description="Second",
                    input_schema={"type": "object"},
                )],
                next_cursor=None,
            )

    monkeypatch.setattr(mcp_client, "Client", FakeClient)
    tools = await McpDiscoveryClient(object(), timeout_seconds=2).list_tools()
    assert [tool.name for tool in tools] == ["first", "second"]
    assert calls == [(None, "bypass"), ("page-2", "bypass")]


async def test_mcp_discovery_rejects_catalog_over_limit():
    import pytest

    from crewspace.infrastructure.mcp_client import McpCatalogTooLarge, McpDiscoveryClient

    server = MCPServer(name="too-large", version="1.0")

    async def first() -> str:
        return "one"

    async def second() -> str:
        return "two"

    server.add_tool(first, name="first")
    server.add_tool(second, name="second")
    with pytest.raises(McpCatalogTooLarge):
        await McpDiscoveryClient(server, timeout_seconds=2, max_tools=1).list_tools()
