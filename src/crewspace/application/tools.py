"""Application: Tool Registry.

A `Tool` is a named, schema-described capability backed by the UnitOfWork. This
is the single catalog consumed by the agent (function calling) AND, later, by
the MCP server — so the agent and the MCP surface never drift from what the app
actually does.

`ToolRegistry` implements the domain `ToolRunner` protocol; `bind(uow)` returns a
request-scoped runner that carries the active UnitOfWork, keeping the agent
ignorant of storage.
"""
from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from ..domain.identifiers import DEFAULT_BOARD_ID, PLANNER_AGENT_ID
from ..domain.ports import ToolRunner, UnitOfWork
from .access import list_accessible_boards


@dataclass
class Tool:
    name: str
    description: str
    input_schema: dict[str, Any]
    # Handler receives the active UnitOfWork plus the parsed arguments.
    handler: Callable[..., Awaitable[Any]]


class _BoundRunner:
    """ToolRunner bound to one UnitOfWork (request scope)."""

    def __init__(
        self, registry: "ToolRegistry", uow: UnitOfWork, principal_id: str | None = None
    ) -> None:
        self._registry = registry
        self._uow = uow
        self._principal_id = principal_id

    async def run(self, tool_name: str, **args: Any) -> dict:
        tool = self._registry.get(tool_name)
        return await tool.handler(self._uow, self._principal_id, **args)


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, Tool] = {}

    def register(self, tool: Tool) -> None:
        self._tools[tool.name] = tool

    def get(self, name: str) -> Tool:
        if name not in self._tools:
            raise KeyError(f"unknown tool: {name}")
        return self._tools[name]

    def list_tools(self) -> list[Tool]:
        return list(self._tools.values())

    def bind(self, uow: UnitOfWork, principal_id: str | None = None) -> ToolRunner:
        return _BoundRunner(self, uow, principal_id)


def build_registry() -> ToolRegistry:
    """Wire the canonical tool set. Add new capabilities here only."""
    reg = ToolRegistry()

    async def require_board_scope(
        uow: UnitOfWork, principal_id: str | None, board_id: str
    ) -> None:
        if principal_id is None:
            return
        from .access import can_access_board

        principal = await uow.auth.get_member(principal_id)
        if not principal or not await can_access_board(principal, board_id, uow):
            raise PermissionError("Principal cannot access this board")

    async def resolve_board_id(
        uow: UnitOfWork, principal_id: str | None, board_id: str | None
    ) -> str:
        """Pick the board to act on, or explain the options.

        - Explicit ``board_id`` is used as-is (caller must still pass scope check).
        - When omitted: a principal with exactly one accessible board gets it
          automatically (so single-team members never need to supply an id).
          A principal with several boards (e.g. superadmin / engineering_manager)
          gets a message listing them (name + id) so the agent can ask which.
        """
        if board_id:
            return board_id
        if principal_id is None:
            # System/agent actions without a principal fall back to the default board.
            return DEFAULT_BOARD_ID
        principal = await uow.auth.get_member(principal_id)
        if not principal:
            return DEFAULT_BOARD_ID
        boards = await list_accessible_boards(principal, uow)
        if len(boards) == 1:
            return boards[0]["id"]
        names = ", ".join(f"{b['name']} ({b['id']})" for b in boards)
        if not boards:
            raise PermissionError("You have no boards available to act on.")
        raise PermissionError(
            "Multiple boards are available — specify which one. " + names
        )

    async def require_channel_scope(
        uow: UnitOfWork, principal_id: str | None, channel_id: str
    ) -> None:
        if principal_id is not None and not await uow.channels.can_member_access(
            channel_id, principal_id
        ):
            raise PermissionError("Principal cannot access this channel")

    async def create_card(uow: UnitOfWork, principal_id: str | None, column_id: str, title: str, description: str | None = None) -> dict:
        board_id = await uow.boards.get_board_id_for_column(column_id)
        if board_id is None:
            raise KeyError(f"column not found: {column_id}")
        await require_board_scope(uow, principal_id, board_id)
        card = await uow.boards.add_card(
            column_id, title, description, actor_id=principal_id or PLANNER_AGENT_ID
        )
        return {"id": card.id, "title": card.title, "column_id": card.column_id}

    async def move_card(uow: UnitOfWork, principal_id: str | None, card_id: str, column_id: str) -> dict:
        board_id = await uow.boards.get_board_id_for_card(card_id)
        target_board_id = await uow.boards.get_board_id_for_column(column_id)
        if board_id is None or target_board_id != board_id:
            raise KeyError(f"card or target column not found: {card_id}")
        await require_board_scope(uow, principal_id, board_id)
        card = await uow.boards.move_card(card_id, column_id, principal_id or PLANNER_AGENT_ID)
        if card is None:
            raise KeyError(f"card not found: {card_id}")
        return {"id": card.id, "title": card.title, "column_id": card.column_id}

    async def comment_card(uow: UnitOfWork, principal_id: str | None, card_id: str, body: str, author_id: str | None = None) -> dict:
        board_id = await uow.boards.get_board_id_for_card(card_id)
        if board_id is None:
            raise KeyError(f"card not found: {card_id}")
        await require_board_scope(uow, principal_id, board_id)
        c = await uow.boards.add_comment(card_id, principal_id or author_id or PLANNER_AGENT_ID, body)
        return {"id": c.id, "card_id": c.card_id, "body": c.body}

    async def find_card(uow: UnitOfWork, principal_id: str | None, title: str, board_id: str | None = None) -> dict | None:
        board_id = await resolve_board_id(uow, principal_id, board_id)
        await require_board_scope(uow, principal_id, board_id)
        card = await uow.boards.find_card_by_title(board_id, title)
        if card is None:
            return None
        return {"id": card.id, "title": card.title, "column_id": card.column_id}

    async def list_columns(uow: UnitOfWork, principal_id: str | None, board_id: str | None = None) -> dict:
        board_id = await resolve_board_id(uow, principal_id, board_id)
        await require_board_scope(uow, principal_id, board_id)
        return await uow.boards.list_columns(board_id)

    async def list_boards(uow: UnitOfWork, principal_id: str | None) -> list[dict]:
        """List the boards available to the caller (id + name + team).

        Call this when a board task needs a target and you don't already know
        which board — e.g. to let a multi-board user pick, or to confirm the
        single board you'll act on.
        """
        if principal_id is None:
            return []
        principal = await uow.auth.get_member(principal_id)
        if not principal:
            return []
        return await list_accessible_boards(principal, uow)

    async def post_message(uow: UnitOfWork, principal_id: str | None, channel_id: str, body: str, author_id: str | None = None) -> dict:
        await require_channel_scope(uow, principal_id, channel_id)
        m = await uow.chat.add_message(
            channel_id, principal_id or author_id or PLANNER_AGENT_ID, body
        )
        return {"id": m.id, "body": m.body, "author_id": m.author_id}

    async def create_cronjob(
        uow: UnitOfWork, principal_id: str | None, name: str, channel_id: str, instruction: str,
        schedule_kind: str, creator_id: str | None = None, interval_value: str | None = None,
        interval_unit: str | None = None, daily_time: str | None = None,
        run_at: str | None = None, description: str | None = None,
    ) -> dict:
        from ..application.scheduling import ScheduledJobService, can_create_channel_job
        from ..config import get_settings
        actor_id = principal_id or creator_id or PLANNER_AGENT_ID
        creator = await uow.auth.get_member(actor_id)
        if not creator or not await can_create_channel_job(creator, channel_id, uow):
            raise PermissionError("Creator cannot manage scheduled instructions for this channel")
        job = await ScheduledJobService(get_settings()).create(
            uow, name=name, description=description, channel_id=channel_id, instruction=instruction,
            schedule_kind=schedule_kind, creator_id=actor_id,
            interval_value=int(interval_value) if interval_value else None,
            interval_unit=interval_unit, daily_time=daily_time, run_at=run_at,
        )
        return {"id": job.id, "channel_id": job.channel_id,
                "instruction": job.instruction, "next_run_at": job.next_run_at.isoformat()}

    reg.register(Tool(
        "create_card", "Create a card in a board column",
        {
            "type": "object",
            "properties": {
                "column_id": {"type": "string", "description": "Target column id, e.g. col_todo|col_doing"},
                "title": {"type": "string", "description": "Card title"},
                "description": {"type": "string", "description": "Optional longer description"},
            },
            "required": ["column_id", "title"],
        },
        create_card,
    ))
    reg.register(Tool(
        "move_card", "Move a card to another column",
        {
            "type": "object",
            "properties": {
                "card_id": {"type": "string", "description": "Card id to move"},
                "column_id": {"type": "string", "description": "Destination column id"},
            },
            "required": ["card_id", "column_id"],
        },
        move_card,
    ))
    reg.register(Tool(
        "comment_card", "Add a comment to a card",
        {
            "type": "object",
            "properties": {
                "card_id": {"type": "string"},
                "body": {"type": "string"},
            },
            "required": ["card_id", "body"],
        },
        comment_card,
    ))
    reg.register(Tool(
        "find_card", "Find a card by its title within a board. If no board_id is given, the agent uses the caller's single board automatically, or lists available boards when there are several.",
        {
            "type": "object",
            "properties": {
                "board_id": {"type": "string", "description": "Optional board id. Omit to use the caller's default board or be shown the available boards."},
                "title": {"type": "string"},
            },
            "required": ["title"],
        },
        find_card,
    ))
    reg.register(Tool(
        "list_columns", "List a board's columns (id + name). If no board_id is given, the agent uses the caller's single board automatically, or lists available boards when there are several.",
        {
            "type": "object",
            "properties": {"board_id": {"type": "string", "description": "Optional board id. Omit to use the caller's default board or be shown the available boards."}},
        },
        list_columns,
    ))
    reg.register(Tool(
        "list_boards", "List the boards available to you (id, name, and team). Call this when a board task needs a target and you don't know which board to use, or to let a multi-board user pick one.",
        {
            "type": "object",
            "properties": {},
        },
        list_boards,
    ))
    reg.register(Tool(
        "post_message", "Post a chat message to a channel",
        {
            "type": "object",
            "properties": {
                "channel_id": {"type": "string"},
                "body": {"type": "string"},
            },
            "required": ["channel_id", "body"],
        },
        post_message,
    ))
    reg.register(Tool(
        "create_cronjob", "Schedule a channel instruction using a human-friendly schedule",
        {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Human-friendly job name"},
                "channel_id": {"type": "string", "description": "Channel where the instruction will be posted"},
                "instruction": {"type": "string", "description": "Message to post; may mention a human or agent"},
                "schedule_kind": {"type": "string", "enum": ["once", "interval", "daily"]},

                "description": {"type": "string", "description": "Optional purpose or context"},
                "interval_value": {"type": "string", "description": "For interval schedules: number of units"},
                "interval_unit": {"type": "string", "enum": ["minutes", "hours", "days"]},
                "daily_time": {"type": "string", "description": "For daily schedules: HH:MM UTC"},
                "run_at": {"type": "string", "description": "For once schedules: ISO date/time"},
            },
            "required": ["name", "channel_id", "instruction", "schedule_kind"],
        },
        create_cronjob,
    ))
    return reg
