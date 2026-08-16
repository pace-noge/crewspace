"""Agent selection + infrastructure package init."""
from __future__ import annotations

from ...config import Settings
from ...domain.ports import AgentProvider
from ...application.tools import ToolRegistry, build_registry
from .llm import LLMAgent
from .stub import StubAgent
from .registry import AgentRegistry, MultiAgentProvider

__all__ = [
    "StubAgent",
    "LLMAgent",
    "AgentRegistry",
    "MultiAgentProvider",
]


def build_agent(settings: Settings) -> AgentProvider:
    """Legacy single-agent factory (kept for scripts/tests). Prefer AgentRegistry.

    Constructs a local agent under the planner identity. For multi-agent
    routing, build via ``AgentRegistry.build(settings, uow)`` inside a request.
    """
    if settings.agent == "llm":
        registry: ToolRegistry = build_registry()
        return LLMAgent.from_registry(registry, api_key=settings.llm_api_key, base_url=settings.llm_base_url, model=settings.llm_model)
    return StubAgent(agent_id="agent_planner", name="Planner", mention="planner")
