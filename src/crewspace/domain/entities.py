"""Domain entities — pure data, no framework or database imports.

These are the in-memory model of the problem space. Nothing here knows about
FastAPI, sqlite, or pydantic. Repositories (infrastructure) are responsible for
turning DB rows into these; services (application) orchestrate them; the API
never sees a raw row.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum


class MemberKind(str, Enum):
    HUMAN = "human"
    AGENT = "agent"


class TeamRole(str, Enum):
    """Roles within a team."""
    LEADER = "leader"
    MEMBER = "member"


class WorkspaceRole(str, Enum):
    """Roles within a workspace."""
    ADMIN = "admin"
    MEMBER = "member"


class ChannelRole(str, Enum):
    """Roles within a channel."""
    ADMIN = "admin"
    MEMBER = "member"


class ChannelType(str, Enum):
    """Channel types."""
    PERMANENT = "permanent"
    TEMPORARY = "temporary"


class ScheduleKind(str, Enum):
    ONCE = "once"
    INTERVAL = "interval"
    DAILY = "daily"


class WorkflowRunStatus(str, Enum):
    RUNNING = "running"
    WAITING = "waiting"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


@dataclass
class CodingRepository:
    id: str
    name: str
    default_branch: str
    created_by: str
    created_at: datetime


@dataclass
class TeamRepositoryAccess:
    team_id: str
    repository_id: str
    granted_by: str
    granted_at: datetime


@dataclass
class CodingRun:
    id: str
    team_id: str
    repository_id: str
    requested_by: str
    agent_id: str
    request_id: str
    instruction: str
    status: str
    created_at: datetime
    updated_at: datetime
    started_at: datetime | None = None
    finished_at: datetime | None = None
    recent_output: str = ""
    failure_reason: str = ""


@dataclass
class StoredChangeSet:
    id: str
    team_id: str
    repository_id: str
    run_id: str
    agent_id: str
    request_id: str
    status: str
    payload: dict
    created_at: datetime


@dataclass
class ChangeSetAuditEvent:
    id: str
    change_set_id: str
    action: str
    actor_id: str
    created_at: datetime


@dataclass
class AgentToolCall:
    id: str
    agent_id: str | None
    initiator_id: str | None
    provider_type: str
    provider_id: str
    tool_name: str
    status: str
    arguments_redacted: str
    created_at: datetime
    result_summary: str | None = None
    error: str | None = None
    duration_ms: int | None = None


@dataclass
class McpConnection:
    id: str
    name: str
    namespace: str
    transport: str
    endpoint_or_command: str
    auth_secret_ref: str | None
    created_by: str
    created_at: datetime
    updated_at: datetime
    enabled: bool = False


@dataclass
class McpDiscoveredTool:
    connection_id: str
    tool_name: str
    description: str
    input_schema: dict
    schema_hash: str
    discovered_at: datetime
    approval_state: str = "pending"


@dataclass
class Member:
    id: str
    kind: MemberKind
    name: str
    avatar: str | None = None


@dataclass
class Channel:
    id: str
    workspace_id: str
    name: str
    topic: str | None = None
    channel_type: ChannelType = ChannelType.PERMANENT
    mention_policy: str = "channel_members"
    created_by: str | None = None
    created_at: datetime | None = None


@dataclass
class Team:
    id: str
    name: str
    created_by: str
    created_at: datetime


@dataclass
class Workspace:
    id: str
    team_id: str
    name: str
    created_by: str
    created_at: datetime


@dataclass
class TeamMembership:
    team_id: str
    member_id: str
    role: TeamRole = TeamRole.MEMBER
    joined_at: datetime | None = None


@dataclass
class WorkspaceMembership:
    workspace_id: str
    member_id: str
    role: WorkspaceRole = WorkspaceRole.MEMBER
    joined_at: datetime | None = None


@dataclass
class ChannelMembership:
    channel_id: str
    member_id: str
    role: ChannelRole = ChannelRole.MEMBER
    joined_at: datetime | None = None
    invited_by: str | None = None
    is_invitation_pending: bool = False


@dataclass
class ScheduledJob:
    id: str
    name: str
    channel_id: str
    instruction: str
    schedule_kind: ScheduleKind
    creator_id: str
    next_run_at: datetime
    interval_value: int | None = None
    interval_unit: str | None = None
    daily_time: str | None = None
    enabled: bool = True
    created_at: datetime | None = None
    last_run_at: datetime | None = None
    last_status: str | None = None
    last_error: str | None = None
    description: str | None = None


@dataclass
class ScheduledJobRun:
    id: str
    job_id: str
    trigger: str
    instruction: str
    channel_id: str
    scheduled_for: datetime
    started_at: datetime
    status: str = "running"
    initiated_by: str | None = None
    finished_at: datetime | None = None
    duration_ms: int | None = None
    message_ids: list[str] = field(default_factory=list)
    error: str | None = None
    next_run_at: datetime | None = None


@dataclass
class Workflow:
    id: str
    name: str
    channel_id: str
    trigger_type: str
    trigger_config: dict
    steps: list[dict]
    creator_id: str
    enabled: bool = True
    description: str | None = None
    filter_expression: str | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None
    next_run_at: datetime | None = None


@dataclass
class WorkflowRun:
    id: str
    workflow_id: str
    trigger_type: str
    event: dict
    status: WorkflowRunStatus
    current_step: int
    step_results: list[dict]
    started_at: datetime
    finished_at: datetime | None = None
    error: str | None = None
    approval_token: str | None = None
    parent_run_id: str | None = None
    root_run_id: str | None = None
    attempt: int = 1
    retry_initiated_by: str | None = None


@dataclass
class Message:
    id: str
    channel_id: str
    author_id: str
    body: str
    created_at: datetime


@dataclass
class Board:
    id: str
    workspace_id: str
    name: str
    archived_at: str | None = None


@dataclass
class Column:
    id: str
    board_id: str
    name: str
    position: int
    archived_at: str | None = None


@dataclass
class Card:
    id: str
    column_id: str
    title: str
    description: str | None = None
    assignee_id: str | None = None
    position: int = 0


@dataclass
class CardRunLink:
    """Durable card ↔ coding-run link, created only from authenticated state.

    ``linked_by`` is the authenticated member who linked the run to the card;
    identity and team scope are derived at the call site, never from agent or
    remote input.
    """

    card_id: str
    run_id: str
    linked_by: str
    linked_at: datetime


@dataclass
class ColumnWorkflowRule:
    """One configurable board-column → workflow mapping."""

    id: str
    board_id: str
    column_id: str
    workflow_id: str
    enabled: bool
    changed_by: str


@dataclass
class ColumnMoveRunStatusView:
    """Live workflow-run projection for a card moved into a trigger column."""

    card_id: str
    workflow_id: str
    workflow_name: str
    run_id: str
    run_status: str


@dataclass
class CardRunStatusView:
    """Live projection of one linked run over its card (joined read model)."""

    card_id: str
    run_id: str
    run_status: str
    change_set_id: str | None = None
    change_set_status: str | None = None
    linked_by: str | None = None
    linked_at: datetime | None = None


@dataclass
class Comment:
    id: str
    card_id: str
    author_id: str
    body: str
    created_at: datetime


# --- Composed read models (projections the UI needs) -----------------------
# Composition over inheritance: these carry the joined author/child data the
# templates render, without polluting the base entities.


@dataclass
class CommentView:
    id: str
    card_id: str
    author_id: str
    body: str
    created_at: datetime
    author_name: str
    author_kind: MemberKind
    author_avatar: str | None = None


@dataclass
class CardView:
    id: str
    column_id: str
    title: str
    description: str | None
    assignee_id: str | None
    position: int
    due_date: str | None = None
    priority: str | None = None
    labels: list[str] = field(default_factory=list)
    assignee_name: str | None = None
    assignee_avatar: str | None = None
    created_by: str | None = None
    updated_by: str | None = None
    updated_at: str | None = None
    created_by_name: str | None = None
    updated_by_name: str | None = None
    comments: list[CommentView] = field(default_factory=list)
    activity: list["CardActivityView"] = field(default_factory=list)


@dataclass
class CardActivityView:
    """An audited field change on a card (used by the detail view + history)."""

    id: str
    card_id: str
    actor_id: str | None
    field: str
    old_value: str | None
    new_value: str | None
    created_at: str
    actor_name: str | None = None


@dataclass
class ColumnView:
    id: str
    board_id: str
    name: str
    position: int
    archived_at: str | None = None
    cards: list[CardView] = field(default_factory=list)


@dataclass
class BoardView:
    id: str
    workspace_id: str
    name: str
    team_name: str | None = None
    archived_at: str | None = None
    columns: list[ColumnView] = field(default_factory=list)


@dataclass
class MessageView:
    id: str
    channel_id: str
    author_id: str
    body: str
    created_at: datetime
    author_name: str
    author_kind: MemberKind
    thread_id: str | None = None
    author_avatar: str | None = None
