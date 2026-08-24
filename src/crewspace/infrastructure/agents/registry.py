"""Infrastructure: agent registry + multi-agent facade.

Slice D, corrected to the Buzz model: an agent is a *separate process on its own
machine that dials INTO the app* over WebSocket (it does NOT wait for the app to
call it). So:

  * ``AgentRegistry``  builds one ``AgentProvider`` per registered agent member.
     A member with no live WebSocket is a LOCAL agent (the in-process Stub/LLM
     logic). A member that is currently connected lives behind ``agent_manager``
     and is reached by pushing frames DOWN its socket.
  * ``MultiAgentProvider`` routes a chat message to the agent that was *mentioned*
     (``@coder``, ``@reviewer`` …) and fans board events out to every connected
     agent — each under its own identity/audit trail, exactly like Buzz.

Local vs remote is resolved at call time by checking ``agent_manager`` liveness,
so an agent can come and go without any app restart or schema change.
"""
from __future__ import annotations

from typing import Any, Awaitable, Callable

from ...config import Settings
from ...domain.identifiers import PLANNER_AGENT_ID
from ...domain.ports import AgentProvider, ToolRunner, UnitOfWork
from ...api.connection import agent_manager
from .stub import StubAgent

MemberLike = Any


def _build_local_agent(
    member: MemberLike, settings: Settings, allowed_tools: list[Any]
) -> AgentProvider:
    """A local agent runs in this process (Stub or LLM logic).

    The agent's ``backend`` column selects stub vs LLM. LLM agents use the
    server's LLM credentials from the environment (``CREWSPACE_LLM_API_KEY`` / ``CREWSPACE_LLM_BASE_URL``)
    — those are never stored in the database, so a DB/backup leak can't expose a key.
    """
    backend = member["backend"] if "backend" in member.keys() else "stub"
    if backend == "llm":
        from .llm import LLMAgent

        return LLMAgent(
            allowed_tools,
            api_key=settings.llm_api_key,
            base_url=settings.llm_base_url,
            model=settings.llm_model,
            agent_id=member["id"],
            name=member["name"],
        )
    return StubAgent(agent_id=member["id"], name=member["name"], mention=member["name"])


class AgentRegistry:
    """Build local providers and mention routing for registered agents.

    Members without a public key are builtin/local and get an in-process
    provider. Members with a public key are remote-only: they are routed over
    WebSocket while connected and reported offline while disconnected.
    """

    @staticmethod
    async def build(
        settings: Settings, uow: UnitOfWork, *, principal_id: str | None = None,
    ) -> "MultiAgentProvider":
        members = await uow.auth.list_members(kind="agent")
        principal = (
            await uow.auth.get_member(principal_id) if principal_id else None
        )
        allow_external = bool(principal and principal["kind"] == "human")
        local: dict[str, AgentProvider] = {}
        mentions: dict[str, str] = {}
        for member in members:
            agent_id = member["id"]
            mentions[member["name"].strip().lstrip("@").lower()] = agent_id
            if not member["pubkey"]:
                from ...application.mcp_tools import list_effective_agent_tools
                from ...application.tools import build_registry

                if allow_external:
                    allowed = await list_effective_agent_tools(
                        build_registry(), uow, agent_id
                    )
                else:
                    native = build_registry()
                    native_grants = await uow.agent_policies.list_enabled_native_tools(
                        agent_id
                    )
                    allowed = [
                        tool for tool in native.list_tools()
                        if tool.name in native_grants
                    ]
                local[agent_id] = _build_local_agent(member, settings, allowed)
        return MultiAgentProvider(
            local, default_agent_id=PLANNER_AGENT_ID, mentions=mentions, settings=settings
        )


class MultiAgentProvider:
    """Route mentions to live remote agents or explicit builtin providers."""

    def __init__(
        self,
        local: dict[str, AgentProvider],
        default_agent_id: str = PLANNER_AGENT_ID,
        mentions: dict[str, str] | None = None,
        settings: Settings | None = None,
    ) -> None:
        # id -> explicit in-process provider (builtin/local agents only)
        self._local = local
        # mention name (lower) -> agent id; includes disconnected remote agents.
        if mentions is not None:
            self._by_mention = dict(mentions)
        else:
            self._by_mention = {}
            for aid, prov in local.items():
                name = getattr(prov, "name", aid)
                self._by_mention[name.strip().lstrip("@").lower()] = aid
        all_ids = set(self._by_mention.values()) | set(local)
        self._default_agent_id = (
            default_agent_id if default_agent_id in all_ids else next(iter(all_ids), "")
        )
        self._settings = settings

    def _resolve(self, text: str) -> str | None:
        low = text.lower()
        for mention, aid in self._by_mention.items():
            if f"@{mention}" in low:
                return aid
        return None

    def resolve(self, text: str) -> str | None:
        """Which agent would answer `text` (without invoking it) — used to show
        a typing indicator before the (possibly slow) agent response arrives."""
        return self._resolve(text)

    async def on_chat_message(
        self, text: str, runner: ToolRunner, context: list[dict[str, str]] | None = None,
        on_engaged: "Callable[[str], Awaitable[None]] | None" = None,
        on_progress: "Callable[[str, str, str], Awaitable[None]] | None" = None,
    ) -> tuple[str, list[str]]:
        aid = self._resolve(text)
        if not aid:
            return ("", [])
        # Connected agent -> push the message DOWN its WebSocket and await its reply.
        if agent_manager.is_connected(aid):
            if on_engaged is not None:
                await on_engaged(aid)
            try:
                async def forward_progress(message_id: str, progress_text: str) -> None:
                    if on_progress is not None:
                        await on_progress(aid, message_id, progress_text)

                payload = {
                    "type": "chat", "agent_id": aid, "text": text, "context": context or []
                }
                timeout = self._settings.agent_reply_timeout if self._settings else 20.0
                if on_progress is not None:
                    reply = await agent_manager.send_and_wait(
                        aid, payload, timeout=timeout, on_progress=forward_progress
                    )
                else:
                    reply = await agent_manager.send_and_wait(aid, payload, timeout=timeout)
                return (aid, [reply] if reply else [])
            except Exception as exc:
                return (aid, [f"⚠️ Agent {aid} did not respond: {exc}"])
        # Not connected -> use the local in-process provider (if any).
        local = self._local.get(aid)
        if local is None:
            return (aid, [f"⚠️ Agent {aid} is offline."])
        return await local.on_chat_message(text, runner, context=context)

    async def on_card_created(
        self,
        card: Any,
        runner: ToolRunner,
        agent_runners: dict[str, ToolRunner] | None = None,
    ) -> None:
        # Push the new-card event to every CONNECTED agent (Buzz-style fan-out);
        # each reacts under its own identity. Local-only agents also get the
        # in-process callback so the seeded planner still drops its note.
        payload = {"type": "card_created", "card": _card_dict(card)}
        connected = agent_manager.connected_agent_ids()
        for aid in connected:
            try:
                await agent_manager.send(aid, payload)
            except Exception:
                continue
        for aid, agent in self._local.items():
            if aid in connected:
                continue
            try:
                scoped_runner = (agent_runners or {}).get(aid, runner)
                await agent.on_card_created(card, scoped_runner)
            except Exception:
                continue


def _card_dict(card: Any) -> dict[str, Any]:
    return {
        "id": card.id,
        "column_id": card.column_id,
        "title": card.title,
        "description": card.description,
        "assignee_id": card.assignee_id,
    }
