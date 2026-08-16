"""Application: workspace service — multi-tenant orchestration.

Handles team/workspace/channel creation, membership, invitations, and
authorization checks. Returns DTOs, never raw rows.
"""
from __future__ import annotations

import datetime as dt
import uuid

from ..domain.entities import (
    Channel,
    ChannelMembership,
    ChannelRole,
    ChannelType,
    Team,
    TeamMembership,
    TeamRole,
    Workspace,
    WorkspaceMembership,
    WorkspaceRole,
)
from ..domain.ports import UnitOfWork
from .access import can_manage_team


class WorkspaceService:
    """Orchestrates team/workspace/channel lifecycle and authorization."""

    def __init__(self) -> None:
        pass

    # --- Teams ----------------------------------------------------------

    async def create_team(
        self, name: str, creator_id: str, uow: UnitOfWork, leader_id: str | None = None
    ) -> Team:
        creator = await uow.auth.get_member(creator_id)
        if not creator or creator["role"] != "superadmin":
            raise PermissionError("Only superadmins can create teams")
        now = dt.datetime.now(dt.timezone.utc)
        team = Team(
            id=f"team_{uuid.uuid4().hex[:8]}",
            name=name,
            created_by=creator_id,
            created_at=now,
        )
        await uow.teams.create_team(team)
        # The selected human becomes this team's leader.
        await uow.teams.add_member(TeamMembership(
            team_id=team.id,
            member_id=leader_id or creator_id,
            role=TeamRole.LEADER,
            joined_at=now,
        ))
        return team

    async def list_teams_for_member(self, member_id: str, uow: UnitOfWork) -> list[Team]:
        return await uow.teams.list_teams_for_member(member_id)

    async def invite_team_member(
        self, team_id: str, inviter_id: str, member_id: str, uow: UnitOfWork
    ) -> TeamMembership:
        """Invite a member to a team. Only team leaders can invite."""
        inviter = await uow.auth.get_member(inviter_id)
        if not inviter or not await can_manage_team(inviter, team_id, uow):
            raise PermissionError("You cannot manage this team")
        membership = TeamMembership(
            team_id=team_id,
            member_id=member_id,
            role=TeamRole.MEMBER,
            joined_at=dt.datetime.now(dt.timezone.utc),
        )
        await uow.teams.add_member(membership)
        return membership

    # --- Workspaces -----------------------------------------------------

    async def create_workspace(
        self, team_id: str, name: str, creator_id: str, uow: UnitOfWork
    ) -> Workspace:
        # Only team leaders can create workspaces
        creator = await uow.auth.get_member(creator_id)
        if not creator or not await can_manage_team(creator, team_id, uow):
            raise PermissionError("You cannot manage this team")
        now = dt.datetime.now(dt.timezone.utc)
        ws = Workspace(
            id=f"ws_{uuid.uuid4().hex[:8]}",
            team_id=team_id,
            name=name,
            created_by=creator_id,
            created_at=now,
        )
        await uow.workspaces.create_workspace(ws)
        # Creator becomes workspace admin
        await uow.workspaces.add_member(WorkspaceMembership(
            workspace_id=ws.id,
            member_id=creator_id,
            role=WorkspaceRole.ADMIN,
            joined_at=now,
        ))
        return ws

    async def list_workspaces_for_member(self, member_id: str, uow: UnitOfWork) -> list[Workspace]:
        return await uow.workspaces.list_workspaces_for_member(member_id)

    async def list_workspaces_for_team(self, team_id: str, member_id: str, uow: UnitOfWork) -> list[Workspace]:
        """List workspaces in a team. Only team members can see them."""
        membership = await uow.teams.get_membership(team_id, member_id)
        if not membership:
            raise PermissionError("Not a member of this team")
        return await uow.workspaces.list_workspaces_for_team(team_id)

    async def invite_workspace_member(
        self, workspace_id: str, inviter_id: str, member_id: str, uow: UnitOfWork
    ) -> WorkspaceMembership:
        """Invite a team member to a workspace. Only workspace admins can invite."""
        if not await uow.workspaces.is_admin(workspace_id, inviter_id):
            raise PermissionError("Only workspace admins can invite members")
        membership = WorkspaceMembership(
            workspace_id=workspace_id,
            member_id=member_id,
            role=WorkspaceRole.MEMBER,
            joined_at=dt.datetime.now(dt.timezone.utc),
        )
        await uow.workspaces.add_member(membership)
        return membership

    # --- Channels -------------------------------------------------------

    async def create_channel(
        self,
        workspace_id: str,
        name: str,
        creator_id: str,
        uow: UnitOfWork,
        channel_type: ChannelType = ChannelType.PERMANENT,
        topic: str | None = None,
        mention_policy: str = "channel_members",
    ) -> Channel:
        workspace = await uow.workspaces.get_workspace(workspace_id)
        if not workspace:
            raise ValueError("Workspace not found")
        creator = await uow.auth.get_member(creator_id)
        if not creator or not await can_manage_team(creator, workspace.team_id, uow):
            raise PermissionError("You cannot manage this team")
        now = dt.datetime.now(dt.timezone.utc)
        channel = Channel(
            id=f"chan_{uuid.uuid4().hex[:8]}",
            workspace_id=workspace_id,
            name=name,
            topic=topic,
            channel_type=channel_type,
            mention_policy=mention_policy,
            created_by=creator_id,
            created_at=now,
        )
        await uow.channels.create_channel(channel)
        # Creator becomes channel admin
        await uow.channels.add_member(ChannelMembership(
            channel_id=channel.id,
            member_id=creator_id,
            role=ChannelRole.ADMIN,
            joined_at=now,
            invited_by=creator_id,
            is_invitation_pending=False,
        ))
        return channel

    async def list_channels_for_workspace(
        self, workspace_id: str, member_id: str, uow: UnitOfWork
    ) -> list[Channel]:
        """List channels in a workspace. Only workspace members can list."""
        ws_membership = await uow.workspaces.get_membership(workspace_id, member_id)
        if not ws_membership:
            raise PermissionError("Not a member of this workspace")
        return await uow.channels.list_channels_for_workspace(workspace_id)

    async def list_channels_for_member(self, member_id: str, uow: UnitOfWork) -> list[Channel]:
        return await uow.channels.list_channels_for_member(member_id)

    async def invite_channel_member(
        self,
        channel_id: str,
        inviter_id: str,
        member_id: str,
        uow: UnitOfWork,
    ) -> ChannelMembership:
        """Assign a human or agent. Team leaders and superadmins manage access."""
        channel = await uow.channels.get_channel(channel_id)
        if not channel:
            raise ValueError("Channel not found")
        workspace = await uow.workspaces.get_workspace(channel.workspace_id)
        if not workspace:
            raise ValueError("Workspace not found")
        inviter = await uow.auth.get_member(inviter_id)
        if not inviter or not await can_manage_team(inviter, workspace.team_id, uow):
            raise PermissionError("You cannot manage this team")
        membership = ChannelMembership(
            channel_id=channel_id,
            member_id=member_id,
            role=ChannelRole.MEMBER,
            joined_at=dt.datetime.now(dt.timezone.utc),
            invited_by=inviter_id,
            is_invitation_pending=False,
        )
        await uow.channels.add_member(membership)
        return membership

    async def accept_invitation(self, channel_id: str, member_id: str, uow: UnitOfWork) -> None:
        """Accept a pending channel invitation."""
        membership = await uow.channels.get_membership(channel_id, member_id)
        if not membership or not membership.is_invitation_pending:
            raise PermissionError("No pending invitation found")
        await uow.channels.update_member_role(channel_id, member_id, membership.role)
        # Update is_invitation_pending to 0
        await uow.channels.add_member(ChannelMembership(
            channel_id=channel_id,
            member_id=member_id,
            role=membership.role,
            joined_at=membership.joined_at or dt.datetime.now(dt.timezone.utc),
            invited_by=membership.invited_by,
            is_invitation_pending=False,
        ))

    # --- Authorization checks -------------------------------------------

    async def can_access_channel(self, channel_id: str, member_id: str, uow: UnitOfWork) -> bool:
        return await uow.channels.can_member_access(channel_id, member_id)

    async def can_mention(
        self, channel_id: str, member_id: str, target_id: str, uow: UnitOfWork
    ) -> bool:
        """Check if a member can mention a target in a channel.

        Rules:
        - Member must have access to the channel
        - Target must be a channel member (for @specific mentions)
        - @all and @everyone are allowed for any channel member
        - Mentioning the agent that registered the channel is allowed
        """
        return await uow.channels.can_member_mention(channel_id, member_id, target_id)

    async def get_channel(self, channel_id: str, member_id: str, uow: UnitOfWork) -> Channel:
        """Get a channel if the member has access."""
        if not await uow.channels.can_member_access(channel_id, member_id):
            raise PermissionError("Not a member of this channel")
        channel = await uow.channels.get_channel(channel_id)
        if not channel:
            raise ValueError("Channel not found")
        return channel

    async def list_channel_members(
        self, channel_id: str, member_id: str, uow: UnitOfWork
    ) -> list[ChannelMembership]:
        """List members of a channel. Only channel members can list."""
        if not await uow.channels.can_member_access(channel_id, member_id):
            raise PermissionError("Not a member of this channel")
        return await uow.channels.list_members(channel_id)
