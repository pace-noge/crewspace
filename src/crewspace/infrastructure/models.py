"""SQLAlchemy persistence models for Crewspace.

These models are infrastructure-only. Repositories map rows to domain entities and
DTO projections; ORM instances never cross the repository boundary.
"""
from __future__ import annotations

from sqlalchemy import CheckConstraint, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class MemberModel(Base):
    __tablename__ = "member"
    __table_args__ = (
        CheckConstraint("kind IN ('human','agent')", name="ck_member_kind"),
        CheckConstraint(
            "role IN ('superadmin','engineering_manager','team_member','agent')",
            name="ck_member_role",
        ),
        CheckConstraint("backend IN ('stub','llm')", name="ck_member_backend"),
    )
    id: Mapped[str] = mapped_column(String, primary_key=True)
    kind: Mapped[str] = mapped_column(String, nullable=False)
    name: Mapped[str] = mapped_column(String, nullable=False)
    avatar: Mapped[str | None] = mapped_column(Text)
    password_hash: Mapped[str | None] = mapped_column(Text)
    role: Mapped[str] = mapped_column(String, nullable=False, default="team_member")
    base_url: Mapped[str | None] = mapped_column(Text)
    pubkey: Mapped[str | None] = mapped_column(Text)
    backend: Mapped[str] = mapped_column(
        String, nullable=False, default="stub", server_default="stub"
    )
    # 1 marks a builtin agent that uses the main app's CREWSPACE_LLM_*
    # credentials. Agent transport is identified separately: pubkey NULL means
    # builtin/local; a non-NULL pubkey means a remote WebSocket agent.
    uses_app_llm: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    archived_at: Mapped[str | None] = mapped_column(String)


class TeamModel(Base):
    __tablename__ = "team"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    name: Mapped[str] = mapped_column(String, nullable=False)
    created_by: Mapped[str] = mapped_column(ForeignKey("member.id"), nullable=False)
    created_at: Mapped[str] = mapped_column(String, nullable=False)
    archived_at: Mapped[str | None] = mapped_column(String)


class TeamMemberModel(Base):
    __tablename__ = "team_member"
    __table_args__ = (CheckConstraint("role IN ('leader','member')", name="ck_team_member_role"),)
    team_id: Mapped[str] = mapped_column(ForeignKey("team.id"), primary_key=True)
    member_id: Mapped[str] = mapped_column(ForeignKey("member.id"), primary_key=True)
    role: Mapped[str] = mapped_column(String, nullable=False, default="member")
    joined_at: Mapped[str] = mapped_column(String, nullable=False)


class WorkspaceModel(Base):
    __tablename__ = "workspace"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    team_id: Mapped[str] = mapped_column(ForeignKey("team.id"), nullable=False)
    name: Mapped[str] = mapped_column(String, nullable=False)
    created_by: Mapped[str] = mapped_column(ForeignKey("member.id"), nullable=False)
    created_at: Mapped[str] = mapped_column(String, nullable=False)
    archived_at: Mapped[str | None] = mapped_column(String)


class WorkspaceMemberModel(Base):
    __tablename__ = "workspace_member"
    __table_args__ = (CheckConstraint("role IN ('admin','member')", name="ck_workspace_member_role"),)
    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspace.id"), primary_key=True)
    member_id: Mapped[str] = mapped_column(ForeignKey("member.id"), primary_key=True)
    role: Mapped[str] = mapped_column(String, nullable=False, default="member")
    joined_at: Mapped[str] = mapped_column(String, nullable=False)


class ChannelModel(Base):
    __tablename__ = "channel"
    __table_args__ = (
        CheckConstraint("channel_type IN ('permanent','temporary')", name="ck_channel_type"),
    )
    id: Mapped[str] = mapped_column(String, primary_key=True)
    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspace.id"), nullable=False)
    name: Mapped[str] = mapped_column(String, nullable=False)
    topic: Mapped[str | None] = mapped_column(Text)
    channel_type: Mapped[str] = mapped_column(String, nullable=False, default="permanent")
    mention_policy: Mapped[str] = mapped_column(String, nullable=False, default="channel_members")
    created_by: Mapped[str] = mapped_column(ForeignKey("member.id"), nullable=False)
    created_at: Mapped[str] = mapped_column(String, nullable=False)
    archived_at: Mapped[str | None] = mapped_column(String)


class ChannelMemberModel(Base):
    __tablename__ = "channel_member"
    __table_args__ = (CheckConstraint("role IN ('admin','member')", name="ck_channel_member_role"),)
    channel_id: Mapped[str] = mapped_column(ForeignKey("channel.id"), primary_key=True)
    member_id: Mapped[str] = mapped_column(ForeignKey("member.id"), primary_key=True)
    role: Mapped[str] = mapped_column(String, nullable=False, default="member")
    joined_at: Mapped[str] = mapped_column(String, nullable=False)
    invited_by: Mapped[str | None] = mapped_column(ForeignKey("member.id"))
    is_invitation_pending: Mapped[int] = mapped_column(Integer, nullable=False, default=0)


class MessageModel(Base):
    __tablename__ = "message"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    channel_id: Mapped[str] = mapped_column(ForeignKey("channel.id"), nullable=False)
    author_id: Mapped[str] = mapped_column(ForeignKey("member.id"), nullable=False)
    body: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[str] = mapped_column(String, nullable=False)
    thread_id: Mapped[str | None] = mapped_column(String)


class MessageReactionModel(Base):
    __tablename__ = "message_reaction"
    message_id: Mapped[str] = mapped_column(ForeignKey("message.id", ondelete="CASCADE"), primary_key=True)
    member_id: Mapped[str] = mapped_column(ForeignKey("member.id", ondelete="CASCADE"), primary_key=True)
    emoji: Mapped[str] = mapped_column(String, primary_key=True)
    created_at: Mapped[str] = mapped_column(String, nullable=False)


class DirectConversationModel(Base):
    __tablename__ = "direct_conversation"
    __table_args__ = (
        UniqueConstraint("member_a_id", "member_b_id", name="uq_direct_members"),
        CheckConstraint("member_a_id < member_b_id", name="ck_direct_member_order"),
    )
    channel_id: Mapped[str] = mapped_column(ForeignKey("channel.id", ondelete="CASCADE"), primary_key=True)
    member_a_id: Mapped[str] = mapped_column(ForeignKey("member.id"), nullable=False)
    member_b_id: Mapped[str] = mapped_column(ForeignKey("member.id"), nullable=False)
    created_at: Mapped[str] = mapped_column(String, nullable=False)


class BoardModel(Base):
    __tablename__ = "board"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspace.id"), nullable=False)
    name: Mapped[str] = mapped_column(String, nullable=False)


class BoardColumnModel(Base):
    __tablename__ = "board_column"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    board_id: Mapped[str] = mapped_column(ForeignKey("board.id"), nullable=False)
    name: Mapped[str] = mapped_column(String, nullable=False)
    position: Mapped[int] = mapped_column(Integer, nullable=False)


class CardModel(Base):
    __tablename__ = "card"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    column_id: Mapped[str] = mapped_column(ForeignKey("board_column.id"), nullable=False)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    assignee_id: Mapped[str | None] = mapped_column(ForeignKey("member.id"))
    position: Mapped[int] = mapped_column(Integer, nullable=False)
    created_by: Mapped[str | None] = mapped_column(ForeignKey("member.id"))
    updated_by: Mapped[str | None] = mapped_column(ForeignKey("member.id"))
    updated_at: Mapped[str | None] = mapped_column(String)


class CardCommentModel(Base):
    __tablename__ = "card_comment"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    card_id: Mapped[str] = mapped_column(ForeignKey("card.id"), nullable=False)
    author_id: Mapped[str] = mapped_column(ForeignKey("member.id"), nullable=False)
    body: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[str] = mapped_column(String, nullable=False)


class SessionModel(Base):
    __tablename__ = "session"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    member_id: Mapped[str] = mapped_column(ForeignKey("member.id"), nullable=False)
    created_at: Mapped[str] = mapped_column(String, nullable=False)


class ScheduledJobModel(Base):
    __tablename__ = "scheduled_job"
    __table_args__ = (
        CheckConstraint("schedule_kind IN ('once','interval','daily')", name="ck_job_schedule_kind"),
    )
    id: Mapped[str] = mapped_column(String, primary_key=True)
    name: Mapped[str] = mapped_column(String, nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    channel_id: Mapped[str] = mapped_column(ForeignKey("channel.id"), nullable=False)
    instruction: Mapped[str] = mapped_column(Text, nullable=False)
    schedule_kind: Mapped[str] = mapped_column(String, nullable=False)
    interval_value: Mapped[int | None] = mapped_column(Integer)
    interval_unit: Mapped[str | None] = mapped_column(String)
    daily_time: Mapped[str | None] = mapped_column(String)
    creator_id: Mapped[str] = mapped_column(ForeignKey("member.id"), nullable=False)
    enabled: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    next_run_at: Mapped[str] = mapped_column(String, nullable=False)
    created_at: Mapped[str] = mapped_column(String, nullable=False)
    last_run_at: Mapped[str | None] = mapped_column(String)
    last_status: Mapped[str | None] = mapped_column(String)
    last_error: Mapped[str | None] = mapped_column(Text)
    claim_token: Mapped[str | None] = mapped_column(String)
    claim_until: Mapped[str | None] = mapped_column(String)


class ScheduledJobRunModel(Base):
    __tablename__ = "scheduled_job_run"
    __table_args__ = (
        CheckConstraint("trigger IN ('manual','scheduled')", name="ck_run_trigger"),
        CheckConstraint("status IN ('running','succeeded','failed','skipped')", name="ck_run_status"),
    )
    id: Mapped[str] = mapped_column(String, primary_key=True)
    job_id: Mapped[str] = mapped_column(ForeignKey("scheduled_job.id"), nullable=False)
    trigger: Mapped[str] = mapped_column(String, nullable=False)
    initiated_by: Mapped[str | None] = mapped_column(ForeignKey("member.id"))
    instruction: Mapped[str] = mapped_column(Text, nullable=False)
    channel_id: Mapped[str] = mapped_column(ForeignKey("channel.id"), nullable=False)
    scheduled_for: Mapped[str] = mapped_column(String, nullable=False)
    started_at: Mapped[str] = mapped_column(String, nullable=False)
    finished_at: Mapped[str | None] = mapped_column(String)
    duration_ms: Mapped[int | None] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String, nullable=False)
    message_ids: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    error: Mapped[str | None] = mapped_column(Text)
    next_run_at: Mapped[str | None] = mapped_column(String)


class WorkflowModel(Base):
    __tablename__ = "workflow"
    __table_args__ = (
        CheckConstraint(
            "trigger_type IN ('message_posted','reaction_added','diff_posted','webhook','schedule')",
            name="ck_workflow_trigger_type",
        ),
        UniqueConstraint("channel_id", "name", name="uq_workflow_channel_name"),
    )
    id: Mapped[str] = mapped_column(String, primary_key=True)
    name: Mapped[str] = mapped_column(String, nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    channel_id: Mapped[str] = mapped_column(ForeignKey("channel.id"), nullable=False)
    enabled: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    trigger_type: Mapped[str] = mapped_column(String, nullable=False)
    trigger_config: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    filter_expression: Mapped[str | None] = mapped_column(Text)
    steps: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    creator_id: Mapped[str] = mapped_column(ForeignKey("member.id"), nullable=False)
    created_at: Mapped[str] = mapped_column(String, nullable=False)
    updated_at: Mapped[str] = mapped_column(String, nullable=False)
    next_run_at: Mapped[str | None] = mapped_column(String)


class WorkflowRunModel(Base):
    __tablename__ = "workflow_run"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    workflow_id: Mapped[str] = mapped_column(ForeignKey("workflow.id", ondelete="CASCADE"), nullable=False)
    trigger_type: Mapped[str] = mapped_column(String, nullable=False)
    event: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(String, nullable=False)
    current_step: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    step_results: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    started_at: Mapped[str] = mapped_column(String, nullable=False)
    finished_at: Mapped[str | None] = mapped_column(String)
    error: Mapped[str | None] = mapped_column(Text)
    approval_token: Mapped[str | None] = mapped_column(String, unique=True)
