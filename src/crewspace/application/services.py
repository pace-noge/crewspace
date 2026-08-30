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
import uuid
from typing import Any

from ..domain.identifiers import DEFAULT_BOARD_ID, DEFAULT_CHANNEL_ID, PLANNER_AGENT_ID
from ..domain.entities import Board
from ..domain.ports import UnitOfWork
from ..domain.entities import CardRunLink, ColumnWorkflowRule
from .access import can_access_board, can_manage_team
from ..dto.board import (
    BoardCommandDTO,
    BoardDTO,
    CardDTO,
    CardRunStatusDTO,
    CardDetailDTO,
    ColumnCommandDTO,
    ColumnDTO,
    ColumnTriggerDTO,
    CommentDTO,
)
from ..dto.mappers import (
    to_board,
    to_card,
    to_card_detail,
    to_card_run_status,
    to_column,
    to_column_trigger,
    to_comment,
    to_message,
)
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
        on_agent_output_complete: "Callable[[str, str], Awaitable[None]] | None" = None,
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
                on_progress_complete=on_agent_output_complete,
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

    async def create_board(self, workspace_id: str, name: str, uow: UnitOfWork) -> BoardDTO:
        """Create a board in a workspace. The caller must have already
        authorized the workspace (see ``can_access_workspace``)."""
        name = BoardCommandDTO(name=name).name
        if not name.strip():
            raise ValueError("board name cannot be empty")
        board_id = f"board_{uuid.uuid4().hex[:8]}"
        await uow.boards.create(Board(id=board_id, workspace_id=workspace_id, name=name.strip()))
        # A brand-new board ships with a sane default column set so cards have
        # somewhere to go (mirrors the seeded board).
        _DEFAULT_NEW_BOARD_COLUMNS = ("To Do", "In Progress", "Done")
        for col_name in _DEFAULT_NEW_BOARD_COLUMNS:
            await uow.boards.create_column(board_id, col_name)
        return to_board(await uow.boards.get_board(board_id))  # type: ignore[return-value]

    async def rename_board(self, board_id: str, name: str, uow: UnitOfWork) -> None:
        name = BoardCommandDTO(name=name).name
        if not name.strip():
            raise ValueError("board name cannot be empty")
        await uow.boards.rename(board_id, name.strip())

    async def archive_board(self, board_id: str, uow: UnitOfWork) -> None:
        await uow.boards.archive(board_id)

    async def restore_board(self, board_id: str, uow: UnitOfWork) -> None:
        await uow.boards.restore(board_id)

    async def list_accessible_boards(self, member_id: str, uow: UnitOfWork) -> list[dict[str, str]]:
        """Boards this member can act on, as {id, name, team} (used by the UI
        switcher and the agent's board resolution). Non-archived only."""
        member = await uow.auth.get_member(member_id)
        if member is not None and member["role"] == "superadmin":
            views = await uow.boards.list_all()
        else:
            views = await uow.boards.list_for_member(member_id)
        return [
            {"id": b.id, "name": b.name, "team": b.team_name or ""}
            for b in views
            if b.archived_at is None
        ]

    async def create_column(self, board_id: str, name: str, uow: UnitOfWork) -> ColumnDTO:
        """Append a column to a board. Caller must have authorized the board."""
        name = ColumnCommandDTO(board_id=board_id, name=name).name
        if not name.strip():
            raise ValueError("column name cannot be empty")
        return to_column(await uow.boards.create_column(board_id, name.strip()))

    async def rename_column(self, column_id: str, name: str, uow: UnitOfWork) -> None:
        name = ColumnCommandDTO(board_id="x", name=name).name
        if not name.strip():
            raise ValueError("column name cannot be empty")
        await uow.boards.rename_column(column_id, name.strip())

    async def reorder_column(self, column_id: str, uow: UnitOfWork, before_column_id: str | None = None) -> None:
        await uow.boards.reorder_column(column_id, before_column_id)

    async def archive_column(self, column_id: str, uow: UnitOfWork) -> None:
        await uow.boards.archive_column(column_id)

    async def restore_column(self, column_id: str, uow: UnitOfWork) -> None:
        await uow.boards.restore_column(column_id)

    async def get_card_detail(self, card_id: str, uow: UnitOfWork) -> CardDetailDTO | None:
        """The full card detail surface (card + edit history)."""
        view = await uow.boards.get_card(card_id)
        if not view:
            return None
        activity = await uow.boards.list_card_activity(card_id)
        return to_card_detail(view, activity)

    async def update_card(
        self,
        card_id: str,
        uow: UnitOfWork,
        *,
        actor_id: str | None = None,
        title: str | None = None,
        description: str | None = None,
        due_date: str | None = None,
        priority: str | None = None,
        labels: list[str] | None = None,
    ) -> CardDTO | None:
        """Patch editable card fields; None leaves a field unchanged (see repository)."""
        if title == "":
            raise ValueError("card title cannot be empty")
        card = await uow.boards.update_card(
            card_id,
            actor_id=actor_id,
            title=title,
            description=description,
            due_date=due_date,
            priority=priority,
            labels=labels,
        )
        return to_card(card) if card else None

    async def set_assignee(
        self, card_id: str, assignee_id: str | None, uow: UnitOfWork, actor_id: str | None = None
    ) -> CardDTO | None:
        card = await uow.boards.set_assignee(card_id, assignee_id, actor_id)
        return to_card(card) if card else None

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
        """Move a card to a new column. Returns (old_column_id, updated_card).

        After a valid, real move into a *different* column, any enabled
        column→workflow rule bound to the target column is triggered (fail-closed:
        the caller must already have authorized the board; the rule must exist and
        be enabled; the referenced workflow must actually exist). Trigger keys are
        idempotent so retried/duplicate moves never double-enqueue.
        """
        old = await uow.boards.get_card(card_id)
        old_column_id = old.column_id if old else None
        card = await uow.boards.move_card(card_id, column_id, actor_id)
        if card is None:
            return old_column_id, None
        if old is not None and old_column_id != column_id:
            from .column_triggers import trigger_column_workflow

            await trigger_column_workflow(
                card=card,
                target_column_id=column_id,
                uow=uow,
                actor_id=actor_id,
                event_key=f"{old.column_id}:{old.updated_at or 'initial'}",
            )
        return old_column_id, to_card(card)

    async def comment_card(
        self, card_id: str, author_id: str, body: str, uow: UnitOfWork
    ) -> CommentDTO:
        return to_comment(await uow.boards.add_comment(card_id, author_id, body))

    async def link_card_to_run(
        self, card_id: str, run_id: str, user: dict, uow: UnitOfWork
    ) -> CardRunStatusDTO:
        """Link a run to a card from authenticated state (fail-closed).

        Authorization is enforced here, never delegated to the repository:
        - the principal must be able to access the card's board;
        - the principal must be able to manage the run's team;
        - the run's team must equal the card's board's team (no cross-team
          linkage).
        Returns the live status projection of the linked run.
        """
        import datetime as _dt

        board_id = await uow.boards.get_board_id_for_card(card_id)
        if board_id is None or not await can_access_board(user, board_id, uow):
            raise PermissionError("Not authorized for this card's board")
        run = await uow.coding_runs.get(run_id)
        if run is None:
            raise KeyError("Coding run not found")
        if not await can_manage_team(user, run.team_id, uow):
            raise PermissionError("Not authorized for the run's team")
        board = await uow.boards.get_board(board_id)
        assert board is not None
        workspace = await uow.workspaces.get_workspace(board.workspace_id)
        if workspace is None or workspace.team_id != run.team_id:
            raise PermissionError("Run team does not match the card's board team")
        await uow.boards.link_card_run(
            CardRunLink(
                card_id=card_id,
                run_id=run.id,
                linked_by=user["id"],
                linked_at=_dt.datetime.now(_dt.timezone.utc),
            )
        )
        statuses = await uow.boards.list_card_run_statuses(card_id)
        status = next((s for s in statuses if s.run_id == run.id), None)
        assert status is not None
        return to_card_run_status(status)

    async def card_run_status(
        self, card_id: str, user: dict, uow: UnitOfWork
    ) -> list[CardRunStatusDTO]:
        """Live status of every run linked to a card (authorization-scoped).

        Reveals nothing when the principal cannot access the card's board —
        the unauthorized caller gets an empty list, never partial data.
        """
        board_id = await uow.boards.get_board_id_for_card(card_id)
        if board_id is None or not await can_access_board(user, board_id, uow):
            return []
        return [
            to_card_run_status(s)
            for s in await uow.boards.list_card_run_statuses(card_id)
        ]

    async def board_run_statuses(
        self, board_id: str, user: dict, uow: UnitOfWork
    ) -> dict[str, list[CardRunStatusDTO]]:
        """Batch live status map {card_id: [CardRunStatusDTO, ...]} for a board.

        Authorization-scoped: an unauthorized principal gets an empty map.
        """
        if not await can_access_board(user, board_id, uow):
            return {}
        by_card: dict[str, list[CardRunStatusDTO]] = {}
        for s in await uow.boards.list_board_run_statuses(board_id):
            by_card.setdefault(s.card_id, []).append(to_card_run_status(s))
        return by_card

    async def set_column_trigger(
        self,
        board_id: str,
        column_id: str,
        workflow_id: str | None,
        enabled: bool,
        user: dict,
        uow: UnitOfWork,
    ) -> ColumnTriggerDTO:
        """Configure (upsert) or clear a board-column → workflow rule.

        Authorization is enforced here, never delegated to the repository: the
        caller must be able to access the board and to manage the board's team.
        A workflow_id of None clears the rule. Referenced workflows must exist.
        """
        if not await can_access_board(user, board_id, uow):
            raise PermissionError("Not authorized for this board")
        board = await uow.boards.get_board(board_id)
        if board is None:
            raise KeyError("Board not found")
        actual_board_id = await uow.boards.get_board_id_for_column(column_id)
        if actual_board_id != board_id:
            raise ValueError("Column does not belong to this board")
        workspace = await uow.workspaces.get_workspace(board.workspace_id)
        if workspace is None or not await can_manage_team(user, workspace.team_id, uow):
            raise PermissionError("Not authorized to manage this board's team")
        if workflow_id is not None:
            workflow = await uow.workflows.get(workflow_id)
            if workflow is None or not workflow.enabled:
                raise ValueError("Workflow not found or disabled")
            workflow_channel = await uow.channels.get_channel(workflow.channel_id)
            if (
                workflow_channel is None
                or workflow_channel.workspace_id != board.workspace_id
            ):
                raise ValueError("Workflow does not belong to this board's workspace")
        if workflow_id is None:
            # Clear: delete any existing rule for this column by disabling it.
            existing = await uow.boards.get_column_workflow(column_id)
            if existing is not None:
                await uow.boards.set_column_workflow(
                    ColumnWorkflowRule(
                        id=existing.id,
                        board_id=board_id,
                        column_id=column_id,
                        workflow_id=existing.workflow_id,
                        enabled=False,
                        changed_by=user["id"],
                    )
                )
                return to_column_trigger(
                    await uow.boards.get_column_workflow(column_id)
                )
            return ColumnTriggerDTO(column_id=column_id, workflow_id=None, enabled=False)
        existing = await uow.boards.get_column_workflow(column_id)
        await uow.boards.set_column_workflow(
            ColumnWorkflowRule(
                id=existing.id if existing else f"cwr_{uuid.uuid4().hex[:12]}",
                board_id=board_id,
                column_id=column_id,
                workflow_id=workflow_id,
                enabled=enabled,
                changed_by=user["id"],
            )
        )
        rule = await uow.boards.get_column_workflow(column_id)
        assert rule is not None
        return to_column_trigger(rule)

    async def list_column_trigger_config(
        self, board_id: str, user: dict, uow: UnitOfWork
    ) -> list[ColumnTriggerDTO]:
        """Per-column → workflow config for one board (authorization-scoped)."""
        if not await can_access_board(user, board_id, uow):
            return []
        return [
            to_column_trigger(r)
            for r in await uow.boards.list_column_workflows(board_id)
        ]
