"""Shared Jinja2 templates instance (api layer)."""
from __future__ import annotations

from pathlib import Path

from fastapi.templating import Jinja2Templates
from ..application.access import can_manage_team, list_accessible_boards
from .connection import agent_manager

_TEMPLATE_DIR = Path(__file__).resolve().parent.parent / "templates"
templates = Jinja2Templates(directory=str(_TEMPLATE_DIR))


async def navigation_context(uow, current_user: dict) -> dict:
    """Build the workspace/channel tree shown in the shared sidebar."""
    teams = await uow.teams.list_teams_for_member(current_user["id"])
    visible_channels = {
        channel.id for channel in await uow.channels.list_channels_for_member(current_user["id"])
    }
    navigation = []
    leads_team = False
    for team in teams:
        workspaces = []
        is_manager = await can_manage_team(current_user, team.id, uow)
        leads_team = leads_team or is_manager
        for workspace in await uow.workspaces.list_workspaces_for_team(team.id):
            channels = await uow.channels.list_channels_for_workspace(workspace.id)
            if not is_manager:
                channels = [channel for channel in channels if channel.id in visible_channels]
            workspaces.append({"workspace": workspace, "channels": channels})
        navigation.append({"team": team, "workspaces": workspaces})
    agents = await uow.auth.list_members(kind="agent")
    agent_statuses = {
        agent["id"]: agent_manager.status(
            agent["id"], is_local=not bool(agent["pubkey"])
        )
        for agent in agents
    }
    agent_profiles = {
        agent["id"]: profile
        for agent in agents
        if (profile := agent_manager.capability_profile(agent["id"])) is not None
    }
    return {
        "workspace_navigation": navigation,
        "direct_messages": await uow.channels.list_direct_for_member(current_user["id"]),
        "boards_menu": await _boards_menu(uow, current_user),
        "can_add_human": current_user["role"] in {"superadmin", "engineering_manager"}
        or leads_team,
        "can_manage": current_user["role"] == "superadmin" or leads_team,
        "agents": agents,
        "agent_statuses": agent_statuses,
        "agent_profiles": agent_profiles,
    }


async def _boards_menu(uow, current_user: dict) -> list[dict[str, str]]:
    """Board switcher entries: LIVE boards the current user can access,
    plus archived boards the user's workspace can restore (recoverable)."""
    live = await list_accessible_boards(current_user, uow)
    # Centralized role-aware list covers LIVE boards; archived boards are
    # recoverable by any workspace member, so fetch membership history too.
    all_boards = (
        await uow.boards.list_all()
        if current_user["role"] == "superadmin"
        else await uow.boards.list_for_member(current_user["id"])
    )
    archived = [
        {"id": b.id, "name": b.name, "team": b.team_name or "", "archived": True}
        for b in all_boards
        if b.archived_at is not None
    ]
    return live + archived
