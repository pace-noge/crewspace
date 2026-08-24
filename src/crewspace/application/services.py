"""Application: services — orchestration that returns DTOs.

Services hold no request state; they receive a `UnitOfWork` per call and return
DTOs (never entities or rows). This is where use-cases live: post a message and
let the (right) agent respond, create a card and let agents react, etc.

Slice D: there is no single hardcoded agent. At call time we build a
`MultiAgentProvider` from the registered agent members (via AgentRegistry), so
chat is routed to the mentioned agent and board events fan out. The services
depend only on the domain ports + DTO boundary — no agent class is imported here.
"""
from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from ..domain.identifiers import DEFAULT_BOARD_ID, DEFAULT_CHANNEL_ID, PLANNER_AGENT_ID
from ..domain.ports import UnitOfWork
from ..dto.board import BoardDTO, CardDTO, ColumnDTO, CommentDTO
from ..dto.mappers import to_board, to_card, to_comment, to_message
from ..dto.messages import MessageDTO
from ..config import Settings
from ..infrastructure.agents.registry import AgentRegistry
from .mcp_tools import (
    McpToolExecutor,
    build_agent_tool_runtime,
    build_unavailable_mcp_executor,
)
from .tools import ToolRegistry


def agent_routable_text(body: str) -> str:
    """Exclude Markdown blockquotes from agent mention routing."""
    return "\n".join(
        line for line in body.splitlines() if not line.lstrip().startswith(">")
    ).strip()


class ChatService:
    def __init__(
        self,
        registry: ToolRegistry,
        settings: Settings,
        *,
        mcp_executor_factory: Callable[[], Awaitable[McpToolExecutor]] = build_unavailable_mcp_executor,
    ) -> None:
        self._registry = registry
        self._settings = settings
        self._mcp_executor_factory = mcp_executor_factory

    async def list_messages(self, channel_id: str, uow: UnitOfWork) -> list[MessageDTO]:
        messages = [to_message(m) for m in await uow.chat.list_messages(channel_id)]
        for message in messages:
            message.reply_count = await uow.chat.thread_reply_count(message.id)
        return messages

    async def list_thread(self, thread_id: str, uow: UnitOfWork) -> list[MessageDTO]:
        return [to_message(m) for m in await uow.chat.list_thread(thread_id)]

    async def announce(self, channel_id: str, body: str, uow: UnitOfWork) -> MessageDTO:
        """Post a planner/system announcement to chat (persisted + returned for broadcast)."""
        msg = await uow.chat.add_message(channel_id, PLANNER_AGENT_ID, body)
        return to_message(msg)

    async def post_and_respond(
        self, channel_id: str, author_id: str, body: str, uow: UnitOfWork,
        thread_id: str | None = None, routing_text: str | None = None,
        on_agent_resolved: "Callable[[str], Awaitable[None]] | None" = None,
        on_human_persisted: "Callable[[MessageDTO], Awaitable[None]] | None" = None,
        on_agent_progress: "Callable[[str], Awaitable[None]] | None" = None,
        on_agent_output: "Callable[[str, str, str], Awaitable[None]] | None" = None,
    ) -> list[MessageDTO]:
        """Persist the human message, route to the mentioned agent, persist its replies."""
        human = await uow.chat.add_message(channel_id, author_id, body, thread_id)
        # Release SQLite's writer lock before any local/remote agent network wait.
        # The inbound message remains durable even when the agent is unavailable.
        await uow.commit()
        human_dto = to_message(human)
        # Echo the human message immediately so the sender sees their own text
        # right away (the agent reply arrives later, in its own frame).
        if on_human_persisted is not None:
            await on_human_persisted(human_dto)
        provider = await AgentRegistry.build(
            self._settings, uow, principal_id=author_id
        )
        routable = agent_routable_text(
            routing_text if routing_text is not None else body
        )
        resolved_agent_id = provider.resolve(routable)
        if resolved_agent_id:
            executor = await self._mcp_executor_factory()
            runtime = await build_agent_tool_runtime(
                self._registry,
                uow,
                principal_id=author_id,
                agent_id=resolved_agent_id,
                executor=executor,
            )
            runner = runtime.runner
        else:
            runner = self._registry.bind(
                uow,
                principal_id=author_id,
                agent_id="",
                allowed_tools=set(),
            )
        # Tell clients which agent is about to answer before the potentially
        # slow local/remote call begins.
        if on_agent_resolved is not None and resolved_agent_id:
            await on_agent_resolved(resolved_agent_id)
        # Build conversation context so the agent can reason over history
        # (e.g. summarize a thread or pull action items). A thread reply sees
        # the whole thread; a channel message sees recent channel history.
        context = await self._build_context(uow, channel_id, thread_id, human.id)
        if on_agent_progress is not None and on_agent_output is not None:
            agent_id, replies = await provider.on_chat_message(
                routable,
                runner,
                context=context,
                on_engaged=on_agent_progress,
                on_progress=on_agent_output,
            )
        elif on_agent_progress is not None:
            agent_id, replies = await provider.on_chat_message(
                routable, runner, context=context, on_engaged=on_agent_progress
            )
        elif on_agent_output is not None:
            agent_id, replies = await provider.on_chat_message(
                routable, runner, context=context, on_progress=on_agent_output
            )
        else:
            agent_id, replies = await provider.on_chat_message(
                routable, runner, context=context
            )
        # Agent answers live in a thread under the human message so the main
        # timeline stays uncluttered (the prior decision: agents reply in thread).
        # Direct messages have no threads, so they stay inline there.
        is_dm = channel_id.startswith("dm_")
        reply_thread_id = thread_id if thread_id else (None if is_dm else human.id)
        agent_msgs = [
            await uow.chat.add_message(channel_id, agent_id, r, reply_thread_id)
            for r in replies if agent_id
        ]
        return [human_dto, *[to_message(a) for a in agent_msgs]]

    async def _build_context(
        self, uow: UnitOfWork, channel_id: str, thread_id: str | None, current_id: str
    ) -> list[dict[str, str]]:
        """Recent conversation the agent should be able to reference.

        Returns up to ~20 prior messages as {"role", "name", "content"} dicts,
        newest first is NOT required — the LLM agent reorders them. The just
        sent message (current_id) is excluded since it is the user's prompt.
        """
        history: list
        if thread_id:
            history = await uow.chat.list_thread(thread_id)
        else:
            history = await uow.chat.list_messages(channel_id, limit=20)
        context: list[dict[str, str]] = []
        for m in history:
            if m.id == current_id:
                continue
            role = "assistant" if m.author_kind == "agent" else "user"
            context.append(
                {"role": role, "name": m.author_name, "content": m.body}
            )
        return context


class BoardService:
    def __init__(self, registry: ToolRegistry, settings: Settings) -> None:
        self._registry = registry
        self._settings = settings

    async def get_board(self, board_id: str, uow: UnitOfWork) -> BoardDTO | None:
        view = await uow.boards.get_board(board_id)
        return to_board(view) if view else None

    async def get_column(self, board_id: str, column_id: str, uow: UnitOfWork) -> ColumnDTO | None:
        """Return a single column (canonical source of truth) re-rendered in order."""
        board = await self.get_board(board_id, uow)
        if board is None:
            return None
        for col in board.columns:
            if col.id == column_id:
                return col
        return None

    async def create_card(
        self, column_id: str, title: str, uow: UnitOfWork, description: str | None = None, actor_id: str | None = None
    ) -> CardDTO:
        card = await uow.boards.add_card(column_id, title, description, actor_id)
        provider = await AgentRegistry.build(self._settings, uow)
        agent_runners = {}
        for member in await uow.auth.list_members(kind="agent"):
            if member["pubkey"]:
                continue
            agent_id = member["id"]
            allowed_tools = await uow.agent_policies.list_enabled_native_tools(
                agent_id
            )
            agent_runners[agent_id] = self._registry.bind(
                uow,
                principal_id=actor_id,
                agent_id=agent_id,
                allowed_tools=allowed_tools,
            )
        await provider.on_card_created(
            card, self._registry.bind_trusted(uow, principal_id=actor_id),
            agent_runners=agent_runners,
        )
        refreshed = await uow.boards.get_card(card.id)
        assert refreshed is not None
        return to_card(refreshed)

    async def move_card(
        self, card_id: str, column_id: str, uow: UnitOfWork, actor_id: str | None = None
    ) -> tuple[str | None, CardDTO | None]:
        """Move a card to a new column. Returns (old_column_id, updated_card)."""
        old = await uow.boards.get_card(card_id)
        old_column_id = old.column_id if old else None
        card = await uow.boards.move_card(card_id, column_id, actor_id)
        return old_column_id, to_card(card) if card else None

    async def comment_card(
        self, card_id: str, author_id: str, body: str, uow: UnitOfWork
    ) -> CommentDTO:
        return to_comment(await uow.boards.add_comment(card_id, author_id, body))
