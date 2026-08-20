"""Application validation for external MCP provider configuration."""
from __future__ import annotations

import os
import re

from ..domain.entities import McpConnection
from ..domain.ports import UnitOfWork

_RESERVED_NAMESPACES = {"crewspace", "native", "system"}
_SECRET_REF = re.compile(r"env:(CREWSPACE_MCP_[A-Z0-9_]+)")
_TRANSPORTS = {"streamable_http", "sse", "stdio_managed"}


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
        if connection.transport not in _TRANSPORTS:
            raise ValueError(f"Unsupported MCP transport {connection.transport!r}")
        if not connection.endpoint_or_command.strip():
            raise ValueError("MCP endpoint or command is required")
        if connection.auth_secret_ref is not None:
            connection.auth_secret_ref = validate_mcp_secret_ref(
                connection.auth_secret_ref
            )
        connection.enabled = False
        return await uow.mcp_connections.create(connection)
