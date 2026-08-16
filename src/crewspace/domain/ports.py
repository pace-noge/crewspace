"""Domain ports (abstractions) — the dependency-inversion seams.

Nothing in domain/ or application/ imports a concrete database driver. They
depend on these protocols. The infrastructure layer provides the implementations
(sqlite today, postgres tomorrow). Swapping the database means writing a new
implementation of `ChatRepository` / `BoardRepository` / `UnitOfWork` — no route,
service, or agent code changes.

`ToolRunner` is the seam an agent uses to act: it calls named tools without
knowing whether they're backed by sqlite, postgres, or an RPC.
"""
from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

from .entities import (
    BoardView,
    CardView,
    Channel,
    ChannelMembership,
    ChannelRole,
    ChannelType,
    CommentView,
    MemberKind,
    MessageView,
    Team,
    TeamMembership,
    TeamRole,
    ScheduledJob,
    ScheduledJobRun,
    Workspace,
    WorkspaceMembership,
    WorkspaceRole,
)


@runtime_checkable
class ToolRunner(Protocol):
    """Run a registered tool by name. The implementation binds a UnitOfWork,
    so the agent never touches storage directly."""

    async def run(self, tool_name: str, **args: Any) -> dict:
        ...


@runtime_checkable
class AgentProvider(Protocol):
    """An agent is a first-class participant: it replies in chat and reacts to
    board events. Implementations live in infrastructure (stub, LLM)."""

    agent_id: str

    async def on_chat_message(self, text: str, runner: ToolRunner) -> tuple[str, list[str]]:
        """Return (responding_agent_id, replies). agent_id is "" if no agent responded."""

    async def on_card_created(self, card: CardView, runner: ToolRunner) -> None:
        ...


@runtime_checkable
class ChatRepository(Protocol):
    async def list_messages(self, channel_id: str) -> list[MessageView]:
        ...

    async def list_thread(self, thread_id: str) -> list[MessageView]:
        ...

    async def thread_reply_count(self, thread_id: str) -> int:
        ...

    async def add_message(
        self, channel_id: str, author_id: str, body: str, thread_id: str | None = None
    ) -> MessageView:
        ...

    async def list_reactions(self, message_id: str, member_id: str) -> list[dict]:
        ...

    async def toggle_reaction(self, message_id: str, member_id: str, emoji: str) -> list[dict]:
        ...


@runtime_checkable
class BoardRepository(Protocol):
    async def get_board(self, board_id: str) -> BoardView | None:
        ...

    async def get_board_id_for_column(self, column_id: str) -> str | None:
        ...

    async def get_board_id_for_card(self, card_id: str) -> str | None:
        ...

    async def add_card(
        self, column_id: str, title: str, description: str | None = None, actor_id: str | None = None
    ) -> CardView:
        ...

    async def get_card(self, card_id: str) -> CardView | None:
        ...

    async def move_card(self, card_id: str, column_id: str, actor_id: str | None = None) -> CardView | None:
        ...

    async def add_comment(
        self, card_id: str, author_id: str, body: str
    ) -> CommentView:
        ...

    async def find_card_by_title(self, board_id: str, title: str) -> CardView | None:
        ...

    async def list_columns(self, board_id: str) -> dict[str, str]:
        """Map of lowercased column name -> column id (for agent commands)."""
        ...


@runtime_checkable
class AuthRepository(Protocol):
    """Members (humans + agents), RBAC roles, and sessions."""

    async def get_member(self, member_id: str) -> Any:
        ...

    async def get_member_by_name(self, name: str) -> Any:
        ...

    async def create_member(
        self,
        member_id: str,
        kind: str,
        name: str,
        password: str | None,
        role: str,
        avatar: str | None = None,
    ) -> None:
        ...

    async def verify_password(self, member_id: str, password: str) -> bool:
        ...

    async def create_session(self, session_id: str, member_id: str) -> None:
        ...

    async def get_session_member(self, session_id: str) -> Any:
        ...

    async def delete_session(self, session_id: str) -> None:
        ...

    async def register_member(
        self, member_id: str, name: str, kind: str, avatar: str | None, role: str, base_url: str | None, pubkey: str | None = None, backend: str = "stub"
    ) -> None:
        ...

    async def get_pubkey(self, member_id: str) -> str | None:
        ...

    async def list_members(self, kind: str | None = None) -> list[Any]:
        ...


@runtime_checkable
class TeamRepository(Protocol):
    async def create_team(self, team: Team) -> Team:
        ...

    async def get_team(self, team_id: str) -> Team | None:
        ...

    async def list_teams(self) -> list[Team]:
        ...

    async def list_teams_for_member(self, member_id: str) -> list[Team]:
        ...

    async def add_member(self, membership: TeamMembership) -> None:
        ...

    async def remove_member(self, team_id: str, member_id: str) -> None:
        ...

    async def get_membership(self, team_id: str, member_id: str) -> TeamMembership | None:
        ...

    async def list_members(self, team_id: str) -> list[TeamMembership]:
        ...

    async def is_leader(self, team_id: str, member_id: str) -> bool:
        ...


@runtime_checkable
class WorkspaceRepository(Protocol):
    async def create_workspace(self, workspace: Workspace) -> Workspace:
        ...

    async def get_workspace(self, workspace_id: str) -> Workspace | None:
        ...

    async def update_name(self, workspace_id: str, name: str) -> None:
        ...

    async def list_workspaces_for_team(self, team_id: str) -> list[Workspace]:
        ...

    async def list_workspaces_for_member(self, member_id: str) -> list[Workspace]:
        ...

    async def add_member(self, membership: WorkspaceMembership) -> None:
        ...

    async def remove_member(self, workspace_id: str, member_id: str) -> None:
        ...

    async def get_membership(self, workspace_id: str, member_id: str) -> WorkspaceMembership | None:
        ...

    async def list_members(self, workspace_id: str) -> list[WorkspaceMembership]:
        ...

    async def is_admin(self, workspace_id: str, member_id: str) -> bool:
        ...


@runtime_checkable
class ChannelRepository(Protocol):
    async def create_channel(self, channel: Channel) -> Channel:
        ...

    async def get_channel(self, channel_id: str) -> Channel | None:
        ...

    async def update_channel(self, channel: Channel) -> None:
        ...

    async def list_channels_for_workspace(self, workspace_id: str) -> list[Channel]:
        ...

    async def list_channels_for_member(self, member_id: str) -> list[Channel]:
        ...

    async def get_or_create_direct(self, member_id: str, peer_id: str) -> Channel:
        ...

    async def get_direct_peer(self, channel_id: str, member_id: str) -> Any:
        ...

    async def list_direct_for_member(self, member_id: str) -> list[dict[str, Any]]:
        ...

    async def add_member(self, membership: ChannelMembership) -> None:
        ...

    async def remove_member(self, channel_id: str, member_id: str) -> None:
        ...

    async def get_membership(self, channel_id: str, member_id: str) -> ChannelMembership | None:
        ...

    async def list_members(self, channel_id: str) -> list[ChannelMembership]:
        ...

    async def can_member_access(self, channel_id: str, member_id: str) -> bool:
        ...

    async def can_member_mention(self, channel_id: str, member_id: str, target_id: str) -> bool:
        ...

    async def update_member_role(self, channel_id: str, member_id: str, role: ChannelRole) -> None:
        ...


@runtime_checkable
class ScheduledJobRepository(Protocol):
    async def create(self, job: ScheduledJob) -> ScheduledJob: ...
    async def get(self, job_id: str) -> ScheduledJob | None: ...
    async def update(self, job: ScheduledJob) -> ScheduledJob: ...
    async def set_enabled(
        self, job_id: str, *, enabled: bool, next_run_at: Any | None = None
    ) -> None: ...
    async def delete(self, job_id: str) -> None: ...
    async def list_for_channels(self, channel_ids: list[str]) -> list[ScheduledJob]: ...
    async def list_due(self, now: Any) -> list[ScheduledJob]: ...
    async def claim_due(
        self, now: Any, *, claim_token: str, claim_until: Any
    ) -> list[ScheduledJob]: ...
    async def record_run(
        self, job_id: str, *, next_run_at: Any, enabled: bool,
        status: str, error: str | None, run_at: Any
    ) -> None: ...
    async def start_run(self, run: ScheduledJobRun) -> ScheduledJobRun: ...
    async def finish_run(
        self, run_id: str, *, status: str, finished_at: Any, duration_ms: int,
        message_ids: list[str], error: str | None, next_run_at: Any
    ) -> None: ...
    async def list_runs(self, job_id: str, limit: int = 100) -> list[ScheduledJobRun]: ...
    async def get_run(self, run_id: str) -> ScheduledJobRun | None: ...


@runtime_checkable
class UnitOfWork(Protocol):
    """Bundles repositories over one consistent storage session."""

    chat: ChatRepository
    boards: BoardRepository
    auth: AuthRepository
    teams: TeamRepository
    workspaces: WorkspaceRepository
    channels: ChannelRepository
    lifecycle: Any
    scheduled_jobs: ScheduledJobRepository

    async def commit(self) -> None:
        ...

    async def rollback(self) -> None:
        ...

    async def close(self) -> None:
        ...
