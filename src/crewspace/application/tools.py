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

import datetime as dt
import json
import re
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from ..domain.identifiers import DEFAULT_BOARD_ID, PLANNER_AGENT_ID
from ..domain.entities import AgentToolCall
from ..domain.ports import ToolRunner, UnitOfWork
from .access import list_accessible_boards


@dataclass
class Tool:
    name: str
    description: str
    input_schema: dict[str, Any]
    # Handler receives the active UnitOfWork plus the parsed arguments.
    handler: Callable[..., Awaitable[Any]]
    provider: str = "crewspace"
    category: str = ""
    mutability: str = "read"
    risk: str = "low"


def native_tool_presets(tools: list[Tool]) -> dict[str, set[str]]:
    """Build native-agent presets from canonical tool metadata."""
    return {
        "none": set(),
        "read_only": {tool.name for tool in tools if tool.mutability == "read"},
        "standard": {tool.name for tool in tools if tool.risk != "high"},
        "all": {tool.name for tool in tools},
    }


class ToolPermissionDenied(PermissionError):
    pass


_SECRET_LABEL = (
    r"authorization|password|passwd|secret|access[_-]?token|refresh[_-]?token|"
    r"token|api[_-]?key|private[_-]?key|ssh[_-]?key|credential|cookie|session|bearer"
)
_SENSITIVE_NAME = re.compile(rf"(?:{_SECRET_LABEL})", re.IGNORECASE)
_SECURITY_HEADER = re.compile(
    r"(?im)\b(authorization|proxy-authorization|cookie|set-cookie)"
    r"\s*:\s*[^\r\n]+"
)
_INLINE_SECRET = re.compile(
    rf"(?i)(?<![A-Za-z0-9])({_SECRET_LABEL})\s*[:=]\s*"
    r"(?:\"[^\"]*\"|'[^']*'|[^\r\n,;&]+)"
)
_BEARER_SECRET = re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]+")
_PEM_SECRET = re.compile(
    r"-----BEGIN [^-\r\n]+-----.*?-----END [^-\r\n]+-----",
    re.DOTALL,
)
_AUDIT_TEXT_LIMIT = 2048


def _redact_string(value: str) -> Any:
    stripped = value.strip()
    if stripped.startswith(("{", "[")):
        try:
            parsed = json.loads(value)
        except (json.JSONDecodeError, TypeError):
            pass
        else:
            if isinstance(parsed, (dict, list)):
                return _redact_value(parsed)
    value = _PEM_SECRET.sub("[REDACTED]", value)
    value = _SECURITY_HEADER.sub(
        lambda match: f"{match.group(1)}: [REDACTED]", value
    )
    value = _BEARER_SECRET.sub("Bearer [REDACTED]", value)
    return _INLINE_SECRET.sub(
        lambda match: f"{match.group(1)}=[REDACTED]", value
    )


def _redact_value(value: Any, key: str | None = None) -> Any:
    if key and _SENSITIVE_NAME.search(key):
        return "[REDACTED]"
    if isinstance(value, dict):
        return {str(k): _redact_value(v, str(k)) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_redact_value(item) for item in value]
    if isinstance(value, str):
        return _redact_string(value)
    return value


def _bounded_audit_text(value: Any) -> str:
    try:
        text = json.dumps(_redact_value(value), default=str, separators=(",", ":"))
    except Exception:
        text = json.dumps("[UNSERIALIZABLE]")
    return text[:_AUDIT_TEXT_LIMIT]


class _BoundRunner:
    """ToolRunner bound to one UnitOfWork and an explicit capability policy."""

    def __init__(
        self, registry: "ToolRegistry", uow: UnitOfWork,
        principal_id: str | None = None, agent_id: str | None = None,
        allowed_tools: set[str] | None = None,
    ) -> None:
        self._registry = registry
        self._uow = uow
        self._principal_id = principal_id
        self._agent_id = agent_id
        self._allowed_tools = allowed_tools

    async def run(self, tool_name: str, **args: Any) -> dict:
        started = time.monotonic()
        call = AgentToolCall(
            id=f"atc_{uuid.uuid4().hex}",
            agent_id=self._agent_id,
            initiator_id=self._principal_id,
            provider_type="native",
            provider_id="crewspace",
            tool_name=tool_name,
            status="allowed",
            arguments_redacted=_bounded_audit_text(args),
            created_at=dt.datetime.now(dt.timezone.utc),
        )
        self._uow.queue_agent_tool_call(call)
        if self._allowed_tools is not None and tool_name not in self._allowed_tools:
            call.status = "blocked"
            call.duration_ms = int((time.monotonic() - started) * 1000)
            call.error = "Tool is not allowed for this agent"
            raise ToolPermissionDenied(
                f"Tool {tool_name!r} is not allowed for agent {self._agent_id!r}"
            )
        try:
            tool = self._registry.get(tool_name)
            actor_id = self._agent_id or self._principal_id
            result = await tool.handler(
                self._uow, self._principal_id, actor_id, **args
            )
        except BaseException as exc:
            call.status = "failed"
            call.duration_ms = int((time.monotonic() - started) * 1000)
            call.error = _bounded_audit_text(f"{type(exc).__name__}: {exc}")
            raise
        call.status = "succeeded"
        call.duration_ms = int((time.monotonic() - started) * 1000)
        call.result_summary = _bounded_audit_text(result)
        return result


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

    def bind(
        self, uow: UnitOfWork, principal_id: str | None = None, *,
        agent_id: str, allowed_tools: set[str],
    ) -> ToolRunner:
        return _BoundRunner(self, uow, principal_id, agent_id, allowed_tools)

    def bind_trusted(
        self, uow: UnitOfWork, principal_id: str | None = None, *,
        agent_id: str | None = None,
    ) -> ToolRunner:
        """Bind an internal adapter that intentionally exposes the full registry."""
        return _BoundRunner(self, uow, principal_id, agent_id)


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

    async def create_card(uow: UnitOfWork, principal_id: str | None, actor_id: str | None, column_id: str, title: str, description: str | None = None) -> dict:
        board_id = await uow.boards.get_board_id_for_column(column_id)
        if board_id is None:
            raise KeyError(f"column not found: {column_id}")
        if await uow.boards.is_column_archived(column_id):
            raise KeyError(f"column is archived: {column_id}")
        await require_board_scope(uow, principal_id, board_id)
        card = await uow.boards.add_card(
            column_id, title, description, actor_id=actor_id or PLANNER_AGENT_ID
        )
        return {"id": card.id, "title": card.title, "column_id": card.column_id}

    async def move_card(uow: UnitOfWork, principal_id: str | None, actor_id: str | None, card_id: str, column_id: str) -> dict:
        board_id = await uow.boards.get_board_id_for_card(card_id)
        target_board_id = await uow.boards.get_board_id_for_column(column_id)
        if board_id is None or target_board_id != board_id:
            raise KeyError(f"card or target column not found: {card_id}")
        if await uow.boards.is_column_archived(column_id):
            raise KeyError(f"target column is archived: {column_id}")
        await require_board_scope(uow, principal_id, board_id)
        card = await uow.boards.move_card(card_id, column_id, actor_id or PLANNER_AGENT_ID)
        if card is None:
            raise KeyError(f"card not found: {card_id}")
        return {"id": card.id, "title": card.title, "column_id": card.column_id}

    async def comment_card(uow: UnitOfWork, principal_id: str | None, actor_id: str | None, card_id: str, body: str, author_id: str | None = None) -> dict:
        board_id = await uow.boards.get_board_id_for_card(card_id)
        if board_id is None:
            raise KeyError(f"card not found: {card_id}")
        await require_board_scope(uow, principal_id, board_id)
        c = await uow.boards.add_comment(card_id, actor_id or author_id or PLANNER_AGENT_ID, body)
        return {"id": c.id, "card_id": c.card_id, "body": c.body}

    async def find_card(uow: UnitOfWork, principal_id: str | None, actor_id: str | None, title: str, board_id: str | None = None) -> dict | None:
        board_id = await resolve_board_id(uow, principal_id, board_id)
        await require_board_scope(uow, principal_id, board_id)
        card = await uow.boards.find_card_by_title(board_id, title)
        if card is None:
            return None
        return {"id": card.id, "title": card.title, "column_id": card.column_id}

    async def get_card(uow: UnitOfWork, principal_id: str | None, actor_id: str | None, card_id: str) -> dict | None:
        board_id = await uow.boards.get_board_id_for_card(card_id)
        if board_id is None:
            raise KeyError(f"card not found: {card_id}")
        await require_board_scope(uow, principal_id, board_id)
        card = await uow.boards.get_card(card_id)
        if card is None:
            return None
        return {
            "id": card.id, "title": card.title, "column_id": card.column_id,
            "description": card.description, "assignee_id": card.assignee_id,
            "assignee_name": card.assignee_name, "due_date": card.due_date,
            "priority": card.priority, "labels": card.labels,
        }

    async def update_card(uow: UnitOfWork, principal_id: str | None, actor_id: str | None, card_id: str,
                          title: str | None = None, description: str | None = None,
                          due_date: str | None = None, priority: str | None = None,
                          labels: list[str] | None = None) -> dict | None:
        """Update a card's metadata (None = keep; empty string = clear optional). Board-scoped."""
        board_id = await uow.boards.get_board_id_for_card(card_id)
        if board_id is None:
            raise KeyError(f"card not found: {card_id}")
        await require_board_scope(uow, principal_id, board_id)
        if priority and priority not in {"low", "medium", "high", "urgent"}:
            raise ValueError(f"invalid priority: {priority}")
        card = await uow.boards.update_card(
            card_id, actor_id=actor_id, title=title, description=description,
            due_date=due_date, priority=priority, labels=labels,
        )
        if card is None:
            return None
        return {
            "id": card.id, "title": card.title, "column_id": card.column_id,
            "description": card.description, "assignee_id": card.assignee_id,
            "due_date": card.due_date, "priority": card.priority, "labels": card.labels,
        }

    async def list_columns(uow: UnitOfWork, principal_id: str | None, actor_id: str | None, board_id: str | None = None) -> dict:
        board_id = await resolve_board_id(uow, principal_id, board_id)
        await require_board_scope(uow, principal_id, board_id)
        return await uow.boards.list_columns(board_id)

    async def list_boards(uow: UnitOfWork, principal_id: str | None, actor_id: str | None) -> list[dict]:
        """List the boards available to the caller (id + name + team).

        Call this when a board task needs a target and you don't know which board
        to use, or to let a multi-board user pick one.
        """
        if principal_id is None:
            # Agent / system context with no specific human principal: surface
            # every board (board *names* only — actual card/column reads stay
            # scope-checked by can_access_board). This keeps list_boards useful
            # for the MCP server, which binds the runner without a principal.
            boards = await uow.boards.list_all()
            return [{"id": b.id, "name": b.name, "team": b.team_name or ""} for b in boards]
        principal = await uow.auth.get_member(principal_id)
        if not principal:
            return []
        return await list_accessible_boards(principal, uow)

    async def post_message(uow: UnitOfWork, principal_id: str | None, actor_id: str | None, channel_id: str, body: str, author_id: str | None = None) -> dict:
        await require_channel_scope(uow, principal_id, channel_id)
        m = await uow.chat.add_message(
            channel_id, actor_id or author_id or PLANNER_AGENT_ID, body
        )
        return {"id": m.id, "body": m.body, "author_id": m.author_id}

    async def create_cronjob(
        uow: UnitOfWork, principal_id: str | None, actor_id: str | None,
        name: str, channel_id: str, instruction: str,
        schedule_kind: str, creator_id: str | None = None, interval_value: str | None = None,
        interval_unit: str | None = None, daily_time: str | None = None,
        run_at: str | None = None, description: str | None = None,
    ) -> dict:
        from ..application.scheduling import ScheduledJobService, can_create_channel_job
        from ..config import get_settings
        authorization_id = principal_id or actor_id or creator_id or PLANNER_AGENT_ID
        creator = await uow.auth.get_member(authorization_id)
        if not creator or not await can_create_channel_job(creator, channel_id, uow):
            raise PermissionError("Creator cannot manage scheduled instructions for this channel")
        execution_actor_id = actor_id or creator_id or PLANNER_AGENT_ID
        job = await ScheduledJobService(get_settings()).create(
            uow, name=name, description=description, channel_id=channel_id, instruction=instruction,
            schedule_kind=schedule_kind, creator_id=execution_actor_id,
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
        category="boards", mutability="write", risk="medium",
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
        category="boards", mutability="write", risk="medium",
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
        category="boards", mutability="write", risk="medium",
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
        category="boards", mutability="read", risk="low",
    ))
    reg.register(Tool(
        "get_card", "Read a card's full metadata by id (board-scoped).",
        {
            "type": "object",
            "properties": {"card_id": {"type": "string"}},
            "required": ["card_id"],
        },
        get_card,
        category="boards", mutability="read", risk="low",
    ))
    reg.register(Tool(
        "update_card", "Update a card's title, description, due date, priority, or labels (board-scoped).",
        {
            "type": "object",
            "properties": {
                "card_id": {"type": "string"},
                "title": {"type": "string"},
                "description": {"type": "string", "description": "Empty string clears the description"},
                "due_date": {"type": "string", "description": "ISO date YYYY-MM-DD; empty string clears"},
                "priority": {"type": "string", "enum": ["", "low", "medium", "high", "urgent"]},
                "labels": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["card_id"],
        },
        update_card,
        category="boards", mutability="write", risk="medium",
    ))
    reg.register(Tool(
        "list_columns", "List a board's columns (id + name). If no board_id is given, the agent uses the caller's single board automatically, or lists available boards when there are several.",
        {
            "type": "object",
            "properties": {"board_id": {"type": "string", "description": "Optional board id. Omit to use the caller's default board or be shown the available boards."}},
        },
        list_columns,
        category="boards", mutability="read", risk="low",
    ))
    reg.register(Tool(
        "list_boards", "List the boards available to you (id, name, and team). Call this when a board task needs a target and you don't know which board to use, or to let a multi-board user pick one.",
        {
            "type": "object",
            "properties": {},
        },
        list_boards,
        category="boards", mutability="read", risk="low",
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
        category="chat", mutability="write", risk="medium",
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
        category="scheduling", mutability="write", risk="high",
    ))
    return reg
