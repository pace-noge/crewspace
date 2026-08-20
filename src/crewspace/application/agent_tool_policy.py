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
        grouped: dict[str, list] = {}
        for tool in tools:
            grouped.setdefault(tool.category, []).append(tool)
        return {
            "agent": agent,
            "groups": grouped,
            "enabled": enabled,
            "presets": native_tool_presets(tools),
        }

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
