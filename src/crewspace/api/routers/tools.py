"""API: tool registry debug endpoint.

Exposes the canonical tool surface (names + JSON schemas) so the agent's and
MCP's capabilities are visible and inspectable. The tool definitions come
straight from the Tool Registry — the single source of truth.
"""
from __future__ import annotations

from fastapi import APIRouter

from ..deps import RegistryDep

router = APIRouter(prefix="/tools", tags=["tools"])


@router.get("")
async def list_tools(registry: RegistryDep) -> dict:
    """List every registered tool with its description + JSON input schema."""
    tools = [
        {
            "name": t.name,
            "description": t.description,
            "input_schema": t.input_schema,
        }
        for t in registry.list_tools()
    ]
    return {"count": len(tools), "tools": tools}
