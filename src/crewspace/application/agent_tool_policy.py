"""Application service for per-agent native tool governance."""
from __future__ import annotations

from typing import Any

from ..domain.ports import UnitOfWork
from .tools import ToolRegistry, native_tool_presets


class AgentToolPolicyService:
    def __init__(self, registry: ToolRegistry) -> None:
        self._registry = registry

    @staticmethod
    def require_superadmin(current_user: dict) -> None:
        if current_user["role"] != "superadmin":
            raise PermissionError("Only superadmins can configure agent tools")

    async def get_builtin_agent(
        self, current_user: dict, agent_id: str, uow: UnitOfWork
    ) -> Any:
        self.require_superadmin(current_user)
        agent = await uow.auth.get_member(agent_id)
        if not agent or agent["kind"] != "agent":
            raise KeyError("agent not found")
        if agent["pubkey"]:
            raise ValueError("Tool settings currently support builtin agents only")
        return agent

    async def view(
        self, current_user: dict, agent_id: str, uow: UnitOfWork
    ) -> dict:
        agent = await self.get_builtin_agent(current_user, agent_id, uow)
        tools = self._registry.list_tools()
        enabled = await uow.agent_policies.list_enabled_native_tools(agent_id)
        enabled_mcp_rows = await uow.agent_policies.list_enabled_mcp_tools(agent_id)
        mcp_tools, _ = await self._active_mcp_catalog(uow)
        grouped: dict[str, list] = {}
        for tool in tools:
            grouped.setdefault(tool.category, []).append(tool)
        return {
            "agent": agent,
            "groups": grouped,
            "enabled": enabled,
            "mcp_tools": mcp_tools,
            "enabled_mcp": {
                qualified for qualified, (_, _, connection_id, tool_name) in mcp_tools.items()
                if (connection_id, tool_name) in enabled_mcp_rows
            },
            "presets": native_tool_presets(tools),
        }

    @staticmethod
    async def _active_mcp_catalog(uow: UnitOfWork) -> tuple[dict, dict]:
        catalog = {}
        selections = {}
        for connection in await uow.mcp_connections.list_connections():
            if not connection.enabled:
                continue
            for tool in await uow.mcp_connections.list_discovered_tools(connection.id):
                if tool.approval_state != "approved":
                    continue
                qualified = f"{connection.namespace}.{tool.tool_name}"
                catalog[qualified] = (
                    tool.description, tool.input_schema, connection.id, tool.tool_name
                )
                selections[qualified] = (connection.id, tool.tool_name)
        return catalog, selections

    async def replace_tool_access(
        self,
        current_user: dict,
        agent_id: str,
        native_names: set[str],
        mcp_names: set[str],
        uow: UnitOfWork,
    ) -> None:
        await self.get_builtin_agent(current_user, agent_id, uow)
        known_native = {tool.name for tool in self._registry.list_tools()}
        unknown_native = native_names - known_native
        if unknown_native:
            raise ValueError("Unknown native tools: " + ", ".join(sorted(unknown_native)))
        _, selections = await self._active_mcp_catalog(uow)
        unknown_mcp = mcp_names - selections.keys()
        if unknown_mcp:
            raise ValueError("Unknown or inactive MCP tools: " + ", ".join(sorted(unknown_mcp)))
        await uow.agent_policies.replace_native_tools(agent_id, native_names)
        await uow.agent_policies.replace_mcp_tools(
            agent_id, {selections[name] for name in mcp_names}
        )
        await uow.commit()

    async def replace_native_tools(
        self,
        current_user: dict,
        agent_id: str,
        tool_names: set[str],
        uow: UnitOfWork,
    ) -> None:
        await self.get_builtin_agent(current_user, agent_id, uow)
        known = {tool.name for tool in self._registry.list_tools()}
        unknown = tool_names - known
        if unknown:
            raise ValueError("Unknown native tools: " + ", ".join(sorted(unknown)))
        await uow.agent_policies.replace_native_tools(agent_id, tool_names)
        await uow.commit()
