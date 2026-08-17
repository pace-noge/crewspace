"""Shared Jinja2 templates instance (api layer)."""
from __future__ import annotations

from pathlib import Path

from fastapi.templating import Jinja2Templates
from ..application.access import can_manage_team
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
    return {
        "workspace_navigation": navigation,
        "direct_messages": await uow.channels.list_direct_for_member(current_user["id"]),
        "can_add_human": current_user["role"] in {"superadmin", "engineering_manager"}
        or leads_team,
        "can_manage": current_user["role"] == "superadmin" or leads_team,
        "agents": agents,
        "agent_statuses": agent_statuses,
    }
