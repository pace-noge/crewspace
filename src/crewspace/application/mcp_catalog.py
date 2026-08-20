"""Transport-independent MCP catalog validation."""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any

from jsonschema.exceptions import SchemaError
from jsonschema.validators import validator_for


class McpDiscoveryError(RuntimeError):
    pass


class McpCatalogTooLarge(McpDiscoveryError):
    pass


_SUPPORTED_SCHEMA_DRAFTS = {
    "http://json-schema.org/draft-04/schema#",
    "http://json-schema.org/draft-06/schema#",
    "http://json-schema.org/draft-07/schema#",
    "https://json-schema.org/draft/2019-09/schema",
    "https://json-schema.org/draft/2020-12/schema",
}


@dataclass(frozen=True)
class DiscoveredMcpTool:
    name: str
    description: str
    input_schema: dict[str, Any]

    @property
    def qualified_name(self) -> str:
        return self.name


def _validate_schema_controls(value: Any) -> None:
    pending = [(value, 0)]
    while pending:
        current, depth = pending.pop()
        if depth > 64:
            raise McpDiscoveryError("MCP input schema exceeds nesting limit")
        if isinstance(current, dict):
            declared_draft = current.get("$schema")
            if declared_draft is not None and (
                not isinstance(declared_draft, str)
                or declared_draft not in _SUPPORTED_SCHEMA_DRAFTS
            ):
                raise McpDiscoveryError(
                    "MCP input schema uses an unsupported schema draft"
                )
            for keyword in ("$ref", "$dynamicRef", "$recursiveRef"):
                reference = current.get(keyword)
                if reference is not None and (
                    not isinstance(reference, str) or not reference.startswith("#")
                ):
                    raise McpDiscoveryError(
                        "MCP input schemas may use only local references"
                    )
            pending.extend((nested, depth + 1) for nested in current.values())
        elif isinstance(current, list):
            pending.extend((nested, depth + 1) for nested in current)


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
        _validate_schema_controls(tool.input_schema)
        try:
            validator_for(tool.input_schema).check_schema(tool.input_schema)
        except SchemaError as exc:
            raise McpDiscoveryError(
                f"MCP tool {tool.name!r} has an invalid input schema"
            ) from exc
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
