"""Application validation for external MCP provider configuration."""
from __future__ import annotations

import os
import re
import datetime as dt
from urllib.parse import urlsplit

from ..domain.entities import McpConnection, McpDiscoveredTool
from ..domain.ports import UnitOfWork
from .mcp_catalog import validate_discovered_tools

_RESERVED_NAMESPACES = {"crewspace", "native", "system"}
_SECRET_REF = re.compile(r"env:(CREWSPACE_MCP_[A-Z0-9_]+)")
_TRANSPORTS = {"streamable_http"}


def validate_mcp_secret_ref(reference: str) -> str:
    reference = reference.strip()
    if not _SECRET_REF.fullmatch(reference):
        raise ValueError(
            "MCP secrets must use env:CREWSPACE_MCP_<NAME> references"
        )
    return reference


def resolve_mcp_secret_ref(reference: str) -> str:
    reference = validate_mcp_secret_ref(reference)
    match = _SECRET_REF.fullmatch(reference)
    assert match is not None
    variable = match.group(1)
    value = os.environ.get(variable)
    if value is None:
        raise KeyError(f"MCP secret environment variable {variable!r} is not set")
    return value


def normalize_mcp_namespace(value: str) -> str:
    namespace = re.sub(r"[^a-z0-9]+", "_", value.strip().lower()).strip("_")
    if not namespace or not re.fullmatch(r"[a-z][a-z0-9_]{0,62}", namespace):
        raise ValueError("MCP namespace must start with a letter and use letters, numbers, or underscores")
    if namespace in _RESERVED_NAMESPACES:
        raise ValueError(f"MCP namespace {namespace!r} is reserved")
    return namespace


class McpConnectionService:
    async def create(
        self, connection: McpConnection, uow: UnitOfWork,
    ) -> McpConnection:
        connection.namespace = normalize_mcp_namespace(connection.namespace)
        if await uow.mcp_connections.get_by_namespace(connection.namespace) is not None:
            raise ValueError(f"MCP namespace {connection.namespace!r} is already in use")
        if connection.transport not in _TRANSPORTS:
            raise ValueError(f"Unsupported MCP transport {connection.transport!r}")
        endpoint = connection.endpoint_or_command.strip()
        parsed = urlsplit(endpoint)
        if parsed.scheme != "https" or not parsed.hostname:
            raise ValueError("External MCP endpoints must use HTTPS")
        if parsed.username is not None or parsed.password is not None:
            raise ValueError("MCP endpoint URLs must not embed credentials")
        if parsed.query or parsed.fragment:
            raise ValueError("MCP endpoint URLs must not contain query or fragment data")
        connection.endpoint_or_command = endpoint
        if connection.auth_secret_ref is not None:
            connection.auth_secret_ref = validate_mcp_secret_ref(
                connection.auth_secret_ref
            )
        connection.enabled = False
        return await uow.mcp_connections.create(connection)


async def discover_mcp_tools(connection: McpConnection, uow: UnitOfWork, *, client):
    discovered = validate_discovered_tools(
        await client.list_tools(),
        max_tools=100,
        max_schema_bytes=64_000,
        max_catalog_bytes=1_000_000,
    )
    persisted = []
    now = dt.datetime.now(dt.timezone.utc)
    for item in discovered:
        tool = McpDiscoveredTool(
            connection_id=connection.id,
            tool_name=item.name,
            description=item.description,
            input_schema=item.input_schema,
            schema_hash="",
            discovered_at=now,
        )
        await uow.mcp_connections.upsert_discovered_tool(tool)
        persisted.append(
            type(item)(
                name=f"{connection.namespace}.{item.name}",
                description=item.description,
                input_schema=item.input_schema,
            )
        )
    await uow.mcp_connections.disable_missing_tools(
        connection.id, {item.name for item in discovered}
    )
    return persisted
