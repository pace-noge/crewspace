"""Bounded MCP discovery adapter."""
from __future__ import annotations

import asyncio
import ipaddress
import json
import re
import socket
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

import httpx2
from httpcore2._backends.auto import AutoBackend
from mcp.client import Client
from mcp.client.streamable_http import streamable_http_client

from ..application.mcp_connections import resolve_mcp_secret_ref
from ..domain.entities import McpConnection


class McpDiscoveryError(RuntimeError):
    pass


class McpCatalogTooLarge(McpDiscoveryError):
    pass


async def _resolve_host(host: str) -> set[str]:
    loop = asyncio.get_running_loop()
    records = await loop.getaddrinfo(host, 443, type=socket.SOCK_STREAM)
    return {str(record[4][0]) for record in records}


_NAT64_NETWORKS = (
    ipaddress.ip_network("64:ff9b::/96"),
    ipaddress.ip_network("64:ff9b:1::/48"),
)


def _is_public_address(value: str) -> bool:
    address = ipaddress.ip_address(value)
    if not address.is_global:
        return False
    if not isinstance(address, ipaddress.IPv6Address):
        return True
    embedded = []
    if address.ipv4_mapped is not None:
        embedded.append(address.ipv4_mapped)
    if address.sixtofour is not None:
        embedded.append(address.sixtofour)
    if address.teredo is not None:
        embedded.extend(address.teredo)
    if any(address in network for network in _NAT64_NETWORKS):
        embedded.append(ipaddress.IPv4Address(int(address) & 0xFFFFFFFF))
    return all(item.is_global for item in embedded)


async def _validate_and_resolve_mcp_endpoint(
    endpoint: str, *, resolver=_resolve_host,
) -> tuple[str, set[str]]:
    parsed = urlsplit(endpoint)
    if parsed.scheme != "https" or not parsed.hostname:
        raise ValueError("External MCP endpoints must use HTTPS")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError("MCP endpoint URLs must not embed credentials")
    if parsed.query or parsed.fragment:
        raise ValueError("MCP endpoint URLs must not contain query or fragment data")
    if parsed.hostname.lower() == "localhost":
        raise ValueError("Loopback MCP endpoints are not allowed")
    try:
        addresses = {str(ipaddress.ip_address(parsed.hostname))}
    except ValueError:
        addresses = await resolver(parsed.hostname)
    if not addresses:
        raise ValueError("MCP endpoint hostname did not resolve")
    if any(not _is_public_address(address) for address in addresses):
        raise ValueError("MCP endpoints must resolve only to public addresses")
    return endpoint, addresses


async def validate_mcp_endpoint(endpoint: str, *, resolver=_resolve_host) -> str:
    validated, _ = await _validate_and_resolve_mcp_endpoint(
        endpoint, resolver=resolver
    )
    return validated


class _PinnedNetworkBackend:
    def __init__(self, addresses: set[str], *, delegate=None) -> None:
        self._addresses = tuple(sorted(addresses))
        self._delegate = delegate or AutoBackend()

    async def connect_tcp(
        self, host, port, timeout=None, local_address=None, socket_options=None,
    ):
        last_error = None
        for address in self._addresses:
            try:
                return await self._delegate.connect_tcp(
                    address, port, timeout, local_address, socket_options
                )
            except Exception as exc:
                last_error = exc
        assert last_error is not None
        raise last_error

    async def connect_unix_socket(self, *args, **kwargs):
        raise RuntimeError("Unix sockets are not supported for external MCP")

    async def sleep(self, seconds):
        await self._delegate.sleep(seconds)


class ExternalMcpDiscoveryClient:
    def __init__(
        self, endpoint: str, authorization_headers: dict[str, str],
        *, pinned_addresses: set[str], timeout_seconds: float = 10,
    ) -> None:
        self.endpoint = endpoint
        self.authorization_headers = authorization_headers
        self.pinned_addresses = pinned_addresses
        self._timeout_seconds = timeout_seconds
        self.follow_redirects = False

    async def list_tools(self) -> list[DiscoveredMcpTool]:
        transport = httpx2.AsyncHTTPTransport(trust_env=False, retries=0)
        # httpx2 has no public DNS-resolver hook. Pin its httpcore2 backend so
        # the validated hostname cannot be rebound between validation and I/O.
        transport._pool._network_backend = _PinnedNetworkBackend(  # pyright: ignore
            self.pinned_addresses
        )
        http_client = httpx2.AsyncClient(
            headers=self.authorization_headers,
            follow_redirects=self.follow_redirects,
            timeout=self._timeout_seconds,
            transport=transport,
            trust_env=False,
        )
        async with http_client:
            transport = streamable_http_client(
                self.endpoint, http_client=http_client
            )
            return await McpDiscoveryClient(
                transport, timeout_seconds=self._timeout_seconds
            ).list_tools()


async def build_external_discovery_client(
    connection: McpConnection, *, resolver=_resolve_host,
) -> ExternalMcpDiscoveryClient:
    if connection.transport != "streamable_http":
        raise ValueError("Only Streamable HTTP MCP connections are supported")
    endpoint, addresses = await _validate_and_resolve_mcp_endpoint(
        connection.endpoint_or_command, resolver=resolver
    )
    headers: dict[str, str] = {}
    if connection.auth_secret_ref:
        headers["Authorization"] = (
            f"Bearer {resolve_mcp_secret_ref(connection.auth_secret_ref)}"
        )
    return ExternalMcpDiscoveryClient(
        endpoint, headers, pinned_addresses=addresses
    )


@dataclass(frozen=True)
class DiscoveredMcpTool:
    name: str
    description: str
    input_schema: dict[str, Any]

    @property
    def qualified_name(self) -> str:
        return self.name


def validate_discovered_tools(
    tools: list[DiscoveredMcpTool], *, max_tools: int,
    max_catalog_bytes: int, max_schema_bytes: int = 64_000,
) -> list[DiscoveredMcpTool]:
    if len(tools) > max_tools:
        raise McpCatalogTooLarge(f"MCP catalog exceeds {max_tools} tools")
    names: set[str] = set()
    total_size = 0
    for tool in tools:
        if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]{0,127}", tool.name):
            raise McpDiscoveryError(f"Unsafe MCP tool name {tool.name!r}")
        if tool.name in names:
            raise McpDiscoveryError(f"Duplicate MCP tool name {tool.name!r}")
        names.add(tool.name)
        if tool.input_schema.get("type") != "object":
            raise McpDiscoveryError(
                f"MCP tool {tool.name!r} must have an object input schema"
            )
        encoded = json.dumps(
            {
                "name": tool.name,
                "description": tool.description,
                "input_schema": tool.input_schema,
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
        schema_size = len(json.dumps(tool.input_schema).encode())
        if schema_size > max_schema_bytes:
            raise McpDiscoveryError(
                f"MCP tool {tool.name!r} schema exceeds size limit"
            )
        total_size += len(encoded)
    if total_size > max_catalog_bytes:
        raise McpCatalogTooLarge("MCP catalog payload exceeds size limit")
    return tools


class McpDiscoveryClient:
    def __init__(
        self,
        server: Any,
        *,
        timeout_seconds: float = 10,
        max_tools: int = 100,
        max_schema_bytes: int = 64_000,
        max_catalog_bytes: int = 1_000_000,
    ) -> None:
        if timeout_seconds <= 0 or max_tools < 1 or max_schema_bytes < 1:
            raise ValueError("MCP discovery limits must be positive")
        self._server = server
        self._timeout_seconds = timeout_seconds
        self._max_tools = max_tools
        self._max_schema_bytes = max_schema_bytes
        self._max_catalog_bytes = max_catalog_bytes

    async def list_tools(self) -> list[DiscoveredMcpTool]:
        raw_tools = []
        catalog_error: McpDiscoveryError | None = None
        try:
            async with asyncio.timeout(self._timeout_seconds):
                async with Client(
                    self._server,
                    read_timeout_seconds=self._timeout_seconds,
                    raise_exceptions=True,
                ) as client:
                    cursor = None
                    seen_cursors: set[str] = set()
                    while True:
                        result = await client.list_tools(
                            cursor=cursor, cache_mode="bypass"
                        )
                        raw_tools.extend(result.tools)
                        if len(raw_tools) > self._max_tools:
                            catalog_error = McpCatalogTooLarge(
                                f"MCP catalog exceeds {self._max_tools} tools"
                            )
                            break
                        cursor = result.next_cursor
                        if cursor is None:
                            break
                        if cursor in seen_cursors:
                            catalog_error = McpDiscoveryError(
                                "MCP catalog returned a repeated pagination cursor"
                            )
                            break
                        seen_cursors.add(cursor)
        except TimeoutError as exc:
            raise McpDiscoveryError("MCP discovery timed out") from exc

        if catalog_error is not None:
            raise catalog_error

        discovered: list[DiscoveredMcpTool] = []
        for tool in raw_tools:
            schema = tool.input_schema
            if not isinstance(schema, dict):
                raise McpDiscoveryError(
                    f"MCP tool {tool.name!r} must have an object input schema"
                )
            discovered.append(
                DiscoveredMcpTool(
                    name=tool.name,
                    description=tool.description or "",
                    input_schema=schema,
                )
            )
        return validate_discovered_tools(
            discovered,
            max_tools=self._max_tools,
            max_schema_bytes=self._max_schema_bytes,
            max_catalog_bytes=self._max_catalog_bytes,
        )
