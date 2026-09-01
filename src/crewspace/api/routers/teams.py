"""Management UI for teams, workspaces, channels, humans, and agents."""
from __future__ import annotations

import datetime as dt
import uuid

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from ...domain.entities import ChannelType, McpConnection, TeamMembership, TeamRole
from ...application.access import can_manage_team, manageable_teams
from ...application.agent_tool_policy import AgentToolPolicyService
from ...application.mcp_connections import McpConnectionService, discover_mcp_tools
from ...application.tools import build_registry
from ...infrastructure.mcp_client import build_external_discovery_client
from ...domain.identifiers import BUILTIN_ASSISTANT_ID
from ..deps import CurrentUserDep, UowDep, WorkspaceServiceDep
from ..rendering import cancel_url, navigation_context, templates
from ..connection import agent_manager

router = APIRouter(prefix="/management", tags=["management"])

MENTION_POLICIES = {
    "all_team": "All team members",
    "channel_members": "Channel members",
    "specific": "Specific channel members",
    "registrar_only": "Only the person who registered the agent",
}


async def _dashboard_context(request: Request, current_user: dict, uow: UowDep) -> dict:
    teams = await manageable_teams(current_user, uow)
    if not teams and current_user["role"] != "superadmin":
        raise HTTPException(status_code=403, detail="You cannot manage teams")
    humans = await uow.auth.list_members(kind="human")
    all_members = await uow.auth.list_members()
    members_by_id = {member["id"]: member for member in all_members}
    hierarchy = []
    for team in teams:
        memberships = await uow.teams.list_members(team.id)
        team_members = [
            {"membership": membership, "member": members_by_id.get(membership.member_id)}
            for membership in memberships
        ]
        workspaces = []
        for workspace in await uow.workspaces.list_workspaces_for_team(team.id):
            channels = []
            for channel in await uow.channels.list_channels_for_workspace(workspace.id):
                channel_members = await uow.channels.list_members(channel.id)
                channels.append({
                    "channel": channel,
                    "members": [
                        {"membership": membership, "member": members_by_id.get(membership.member_id)}
                        for membership in channel_members
                    ],
                })
            workspaces.append({"workspace": workspace, "channels": channels})
        hierarchy.append({"team": team, "members": team_members, "workspaces": workspaces})
    archived_items = await uow.lifecycle.list_archived()
    if current_user["role"] != "superadmin":
        managed_team_ids = {team.id for team in teams}
        scoped_items = []
        for item in archived_items:
            if item["kind"] == "agent":
                continue
            if await uow.lifecycle.team_scope(item["kind"], item["id"]) in managed_team_ids:
                scoped_items.append(item)
        archived_items = scoped_items
    return {
        "request": request,
        "current_user": current_user,
        "agents": await uow.auth.list_members(kind="agent"),
        "humans": humans,
        "all_members": all_members,
        "hierarchy": hierarchy,
        "mention_policies": MENTION_POLICIES,
        "archived_items": archived_items,
        **await navigation_context(uow, current_user),
    }


async def _render_dashboard(request: Request, current_user: dict, uow: UowDep, **headers: str):
    context = await _dashboard_context(request, current_user, uow)
    return templates.TemplateResponse(
        request=request,
        name="management.html",
        context=context,
        headers=headers or None,
    )


def _forbidden(exc: PermissionError) -> HTTPException:
    return HTTPException(status_code=403, detail=str(exc))


def _require_superadmin(current_user: dict) -> None:
    if current_user["role"] != "superadmin":
        raise HTTPException(
            status_code=403, detail="Only superadmins can manage MCP connections"
        )


LIFECYCLE_KINDS = {
    "teams": "team", "workspaces": "workspace", "channels": "channel", "agents": "agent"
}


async def _lifecycle_entity(collection: str, entity_id: str, current_user: dict, uow: UowDep):
    kind = LIFECYCLE_KINDS.get(collection)
    if kind is None:
        raise HTTPException(status_code=404, detail="Not found")
    entity = await uow.lifecycle.get(kind, entity_id)
    if entity is None:
        raise HTTPException(status_code=404, detail=f"{kind.title()} not found")
    if kind == "agent":
        if current_user["role"] != "superadmin":
            raise HTTPException(status_code=403, detail="Only superadmins can manage agent lifecycle")
    else:
        team_id = await uow.lifecycle.team_scope(kind, entity_id)
        can_manage = (
            current_user["role"] == "superadmin"
            or (
                team_id is not None
                and await uow.lifecycle.can_manage_archived_team(
                    team_id, current_user["id"], current_user["role"]
                )
            )
        )
        if team_id is None or not can_manage:
            raise HTTPException(status_code=403, detail=f"You cannot manage this {kind}")
    return kind, entity


@router.post("/{collection}/{entity_id}/archive")
async def archive_entity(
    collection: str, entity_id: str, current_user: CurrentUserDep, uow: UowDep
) -> RedirectResponse:
    kind, _ = await _lifecycle_entity(collection, entity_id, current_user, uow)
    if kind == "agent" and entity_id == BUILTIN_ASSISTANT_ID:
        raise HTTPException(status_code=403, detail="The builtin assistant cannot be archived or deleted")
    if kind == "agent":
        await agent_manager.close(entity_id)
    await uow.lifecycle.set_archived(kind, entity_id, True)
    await uow.commit()
    return RedirectResponse("/management", status_code=303)


@router.post("/{collection}/{entity_id}/restore")
async def restore_entity(
    collection: str, entity_id: str, current_user: CurrentUserDep, uow: UowDep
) -> RedirectResponse:
    kind, _ = await _lifecycle_entity(collection, entity_id, current_user, uow)
    if kind == "agent" and entity_id == BUILTIN_ASSISTANT_ID:
        raise HTTPException(status_code=403, detail="The builtin assistant cannot be archived or deleted")
    await uow.lifecycle.set_archived(kind, entity_id, False)
    await uow.commit()
    return RedirectResponse("/management", status_code=303)


@router.get("/{collection}/{entity_id}/delete", response_class=HTMLResponse)
async def delete_entity_page(
    request: Request, collection: str, entity_id: str,
    current_user: CurrentUserDep, uow: UowDep,
):
    if current_user["role"] != "superadmin":
        raise HTTPException(status_code=403, detail="Only superadmins can permanently delete")
    kind, entity = await _lifecycle_entity(collection, entity_id, current_user, uow)
    if kind == "agent" and entity_id == BUILTIN_ASSISTANT_ID:
        raise HTTPException(status_code=403, detail="The builtin assistant cannot be deleted")
    # Return to the page the action was opened from; reject foreign referers.
    back = cancel_url(request, "/management")
    return templates.TemplateResponse(
        request=request, name="delete_confirm.html",
        context={
            "request": request, "current_user": current_user,
            "agents": await uow.auth.list_members(kind="agent"),
            "kind": kind, "collection": collection, "entity": entity,
            "counts": await uow.lifecycle.dependency_counts(kind, entity_id),
            "back": back,
            **await navigation_context(uow, current_user),
        },
    )


@router.post("/{collection}/{entity_id}/delete")
async def delete_entity(
    collection: str, entity_id: str, current_user: CurrentUserDep, uow: UowDep,
    confirmation: str = Form(...), back: str = Form(None),
) -> RedirectResponse:
    if current_user["role"] != "superadmin":
        raise HTTPException(status_code=403, detail="Only superadmins can permanently delete")
    kind, entity = await _lifecycle_entity(collection, entity_id, current_user, uow)
    if kind == "agent" and entity_id == BUILTIN_ASSISTANT_ID:
        raise HTTPException(status_code=403, detail="The builtin assistant cannot be deleted")
    if confirmation.strip() != entity["name"]:
        raise HTTPException(status_code=422, detail="Confirmation name does not match")
    if kind == "agent":
        await agent_manager.close(entity_id)
    await uow.lifecycle.delete_permanently(kind, entity_id)
    await uow.commit()
    redirect_to = back if back and back.startswith("/") else "/management"
    return RedirectResponse(redirect_to, status_code=303)


async def _workspace_form_context(
    request: Request, current_user: dict, uow: UowDep, workspace_id: str, mode: str
) -> dict:
    workspace = await uow.workspaces.get_workspace(workspace_id)
    if workspace is None:
        raise HTTPException(status_code=404, detail="Workspace not found")
    if not await can_manage_team(current_user, workspace.team_id, uow):
        raise HTTPException(status_code=403, detail="You cannot manage this workspace")
    channels = await uow.channels.list_channels_for_workspace(workspace_id)
    return {
        "request": request,
        "current_user": current_user,
        "agents": await uow.auth.list_members(kind="agent"),
        "workspace": workspace,
        "mode": mode,
        "title": "Manage workspace" if mode == "manage" else "Add channel",
        "first_channel_id": channels[0].id if channels else "chan_general",
        "mention_policies": MENTION_POLICIES,
        **await navigation_context(uow, current_user),
    }


async def _channel_form_context(
    request: Request, current_user: dict, uow: UowDep, channel_id: str
) -> dict:
    channel = await uow.channels.get_channel(channel_id)
    if channel is None:
        raise HTTPException(status_code=404, detail="Channel not found")
    workspace = await uow.workspaces.get_workspace(channel.workspace_id)
    if workspace is None:
        raise HTTPException(status_code=404, detail="Workspace not found")
    if not await can_manage_team(current_user, workspace.team_id, uow):
        raise HTTPException(status_code=403, detail="You cannot manage this channel")
    return {
        "request": request,
        "current_user": current_user,
        "agents": await uow.auth.list_members(kind="agent"),
        "channel": channel,
        "workspace": workspace,
        "mention_policies": MENTION_POLICIES,
        **await navigation_context(uow, current_user),
    }


@router.get("", response_class=HTMLResponse)
async def management_dashboard(request: Request, current_user: CurrentUserDep, uow: UowDep):
    return await _render_dashboard(request, current_user, uow)


@router.get("/workspaces", response_class=HTMLResponse)
async def manage_workspaces_page(
    request: Request, current_user: CurrentUserDep, uow: UowDep
):
    teams = await manageable_teams(current_user, uow)
    if not teams and current_user["role"] != "superadmin":
        raise HTTPException(status_code=403, detail="You cannot manage workspaces")
    groups = []
    for team in teams:
        groups.append({
            "team": team,
            "workspaces": await uow.workspaces.list_workspaces_for_team(team.id),
        })
    return templates.TemplateResponse(
        request=request,
        name="workspaces.html",
        context={
            "request": request,
            "current_user": current_user,
            "agents": await uow.auth.list_members(kind="agent"),
            "workspace_groups": groups,
            **await navigation_context(uow, current_user),
        },
    )


@router.get("/workspaces/new", response_class=HTMLResponse)
async def add_workspace_page(
    request: Request, current_user: CurrentUserDep, uow: UowDep
):
    teams = await manageable_teams(current_user, uow)
    if not teams:
        raise HTTPException(status_code=403, detail="You cannot add workspaces")
    return templates.TemplateResponse(
        request=request,
        name="workspace_create.html",
        context={
            "request": request,
            "current_user": current_user,
            "teams": teams,
            "agents": await uow.auth.list_members(kind="agent"),
            **await navigation_context(uow, current_user),
        },
    )


@router.post("/workspaces")
async def create_workspace_from_form(
    current_user: CurrentUserDep,
    svc: WorkspaceServiceDep,
    uow: UowDep,
    name: str = Form(...),
    team_id: str = Form(...),
) -> RedirectResponse:
    allowed_team_ids = {team.id for team in await manageable_teams(current_user, uow)}
    if team_id not in allowed_team_ids:
        raise HTTPException(status_code=403, detail="You cannot add a workspace to that team")
    clean_name = name.strip()
    if not clean_name:
        raise HTTPException(status_code=422, detail="Workspace name is required")
    try:
        workspace = await svc.create_workspace(team_id, clean_name, current_user["id"], uow)
    except PermissionError as exc:
        raise _forbidden(exc) from exc
    await uow.commit()
    return RedirectResponse(f"/management/workspaces/{workspace.id}", status_code=303)


@router.get("/workspaces/{workspace_id}", response_class=HTMLResponse)
async def manage_workspace_page(
    request: Request, workspace_id: str, current_user: CurrentUserDep, uow: UowDep
):
    return templates.TemplateResponse(
        request=request, name="workspace_form.html",
        context=await _workspace_form_context(request, current_user, uow, workspace_id, "manage"),
    )


@router.post("/workspaces/{workspace_id}", response_class=HTMLResponse)
async def update_workspace(
    request: Request,
    workspace_id: str,
    current_user: CurrentUserDep,
    uow: UowDep,
    name: str = Form(...),
):
    await _workspace_form_context(request, current_user, uow, workspace_id, "manage")
    clean_name = name.strip()
    if not clean_name:
        raise HTTPException(status_code=422, detail="Workspace name is required")
    await uow.workspaces.update_name(workspace_id, clean_name)
    await uow.commit()
    return await _render_dashboard(request, current_user, uow)


@router.get("/workspaces/{workspace_id}/channels/new", response_class=HTMLResponse)
async def add_channel_page(
    request: Request, workspace_id: str, current_user: CurrentUserDep, uow: UowDep
):
    return templates.TemplateResponse(
        request=request, name="workspace_form.html",
        context=await _workspace_form_context(request, current_user, uow, workspace_id, "channel"),
    )


@router.get("/channels/{channel_id}", response_class=HTMLResponse)
async def manage_channel_page(
    request: Request, channel_id: str, current_user: CurrentUserDep, uow: UowDep
):
    return templates.TemplateResponse(
        request=request, name="channel_form.html",
        context=await _channel_form_context(request, current_user, uow, channel_id),
    )


@router.post("/channels/{channel_id}", response_class=HTMLResponse)
async def update_channel_settings(
    request: Request,
    channel_id: str,
    current_user: CurrentUserDep,
    uow: UowDep,
    name: str = Form(...),
    topic: str = Form(""),
    channel_type: str = Form("permanent"),
    mention_policy: str = Form("channel_members"),
):
    context = await _channel_form_context(request, current_user, uow, channel_id)
    if mention_policy not in MENTION_POLICIES:
        raise HTTPException(status_code=422, detail="Invalid mention policy")
    clean_name = name.strip()
    if not clean_name:
        raise HTTPException(status_code=422, detail="Channel name is required")
    channel = context["channel"]
    channel.name = clean_name
    channel.topic = topic.strip() or None
    channel.channel_type = ChannelType(channel_type)
    channel.mention_policy = mention_policy
    await uow.channels.update_channel(channel)
    await uow.commit()
    return await _render_dashboard(request, current_user, uow)


@router.get("/channels/{channel_id}/members", response_class=HTMLResponse)
async def manage_channel_members_page(
    request: Request, channel_id: str, current_user: CurrentUserDep, uow: UowDep
):
    context = await _channel_form_context(request, current_user, uow, channel_id)
    all_members = await uow.auth.list_members()
    members_by_id = {member["id"]: member for member in all_members}
    context["all_members"] = all_members
    context["channel_members"] = [
        {"membership": membership, "member": members_by_id[membership.member_id]}
        for membership in await uow.channels.list_members(channel_id)
        if membership.member_id in members_by_id
    ]
    return templates.TemplateResponse(
        request=request, name="channel_members.html", context=context
    )


async def _human_team_scope(current_user: dict, uow: UowDep):
    teams = await manageable_teams(current_user, uow)
    if not teams:
        raise HTTPException(status_code=403, detail="You cannot add humans")
    must_choose = current_user["role"] in {"superadmin", "engineering_manager"} or len(teams) != 1
    return teams, must_choose


@router.get("/humans/new", response_class=HTMLResponse)
async def add_human_page(
    request: Request, current_user: CurrentUserDep, uow: UowDep
):
    teams, choose_team = await _human_team_scope(current_user, uow)
    if not teams:
        raise HTTPException(status_code=403, detail="No managed teams available")
    return templates.TemplateResponse(
        request=request,
        name="human_form.html",
        context={
            "request": request,
            "current_user": current_user,
            "agents": await uow.auth.list_members(kind="agent"),
            "teams": teams,
            "choose_team": choose_team,
            "automatic_team": teams[0] if not choose_team else None,
            "cancel_url": cancel_url(request, "/management"),
            **await navigation_context(uow, current_user),
        },
    )


@router.post("/humans", response_class=HTMLResponse)
async def create_human(
    request: Request,
    current_user: CurrentUserDep,
    uow: UowDep,
    name: str = Form(...),
    password: str = Form(...),
    team_id: str | None = Form(None),
):
    teams, choose_team = await _human_team_scope(current_user, uow)
    allowed_team_ids = {team.id for team in teams}
    selected_team_id = team_id if choose_team else (teams[0].id if teams else None)
    if not selected_team_id or selected_team_id not in allowed_team_ids:
        raise HTTPException(status_code=403, detail="You cannot add a human to that team")
    clean_name = name.strip()
    if not clean_name:
        raise HTTPException(status_code=422, detail="Name is required")
    if await uow.auth.get_member_by_name(clean_name):
        raise HTTPException(status_code=409, detail="A member with that name already exists")
    import uuid

    member_id = f"user_{uuid.uuid4().hex[:8]}"
    await uow.auth.create_member(
        member_id, "human", clean_name, password, "team_member", avatar="🧑"
    )
    await uow.teams.add_member(TeamMembership(
        team_id=selected_team_id, member_id=member_id, role=TeamRole.MEMBER
    ))
    await uow.commit()
    return await _render_dashboard(request, current_user, uow)


@router.post("/teams", response_class=HTMLResponse)
async def create_team(
    request: Request,
    current_user: CurrentUserDep,
    svc: WorkspaceServiceDep,
    uow: UowDep,
    name: str = Form(...),
    leader_id: str = Form(...),
):
    try:
        await svc.create_team(name.strip(), current_user["id"], uow, leader_id=leader_id)
    except PermissionError as exc:
        raise _forbidden(exc) from exc
    await uow.commit()
    return await _render_dashboard(request, current_user, uow)


@router.post("/teams/{team_id}/members", response_class=HTMLResponse)
async def add_team_member(
    request: Request,
    team_id: str,
    current_user: CurrentUserDep,
    svc: WorkspaceServiceDep,
    uow: UowDep,
    member_id: str = Form(...),
):
    try:
        await svc.invite_team_member(team_id, current_user["id"], member_id, uow)
    except PermissionError as exc:
        raise _forbidden(exc) from exc
    await uow.commit()
    return await _render_dashboard(request, current_user, uow)


@router.post("/teams/{team_id}/workspaces", response_class=HTMLResponse)
async def create_workspace(
    request: Request,
    team_id: str,
    current_user: CurrentUserDep,
    svc: WorkspaceServiceDep,
    uow: UowDep,
    name: str = Form(...),
):
    try:
        workspace = await svc.create_workspace(team_id, name.strip(), current_user["id"], uow)
    except PermissionError as exc:
        raise _forbidden(exc) from exc
    await uow.commit()
    return await _render_dashboard(request, current_user, uow, **{"x-workspace-id": workspace.id})


@router.post("/workspaces/{workspace_id}/channels", response_class=HTMLResponse)
async def create_channel(
    request: Request,
    workspace_id: str,
    current_user: CurrentUserDep,
    svc: WorkspaceServiceDep,
    uow: UowDep,
    name: str = Form(...),
    topic: str = Form(""),
    channel_type: str = Form("permanent"),
    mention_policy: str = Form("channel_members"),
):
    if mention_policy not in MENTION_POLICIES:
        raise HTTPException(status_code=422, detail="Invalid mention policy")
    try:
        await svc.create_channel(
            workspace_id,
            name.strip(),
            current_user["id"],
            uow,
            ChannelType(channel_type),
            topic.strip() or None,
            mention_policy,
        )
    except PermissionError as exc:
        raise _forbidden(exc) from exc
    await uow.commit()
    return await _render_dashboard(request, current_user, uow)


@router.post("/channels/{channel_id}/members", response_class=HTMLResponse)
async def assign_channel_member(
    request: Request,
    channel_id: str,
    current_user: CurrentUserDep,
    svc: WorkspaceServiceDep,
    uow: UowDep,
    member_id: str = Form(...),
):
    if not await uow.auth.get_member(member_id):
        raise HTTPException(status_code=404, detail="Member not found")
    try:
        await svc.invite_channel_member(channel_id, current_user["id"], member_id, uow)
    except PermissionError as exc:
        raise _forbidden(exc) from exc
    await uow.commit()
    return await _render_dashboard(request, current_user, uow)


@router.post("/channels/{channel_id}/members/{member_id}/remove", response_class=HTMLResponse)
async def remove_channel_member(
    request: Request,
    channel_id: str,
    member_id: str,
    current_user: CurrentUserDep,
    uow: UowDep,
):
    channel = await uow.channels.get_channel(channel_id)
    workspace = await uow.workspaces.get_workspace(channel.workspace_id) if channel else None
    if not workspace or not await can_manage_team(current_user, workspace.team_id, uow):
        raise HTTPException(status_code=403, detail="You cannot manage channel access")
    await uow.channels.remove_member(channel_id, member_id)
    await uow.commit()
    return await _render_dashboard(request, current_user, uow)


@router.get("/mcp", response_class=HTMLResponse)
async def mcp_connections_page(
    request: Request, current_user: CurrentUserDep, uow: UowDep,
):
    _require_superadmin(current_user)
    return templates.TemplateResponse(
        request=request,
        name="mcp_connections.html",
        context={
            "request": request,
            "current_user": current_user,
            "connections": await uow.mcp_connections.list_connections(),
            **await navigation_context(uow, current_user),
        },
    )


@router.get("/mcp/new", response_class=HTMLResponse)
async def new_mcp_connection_page(
    request: Request, current_user: CurrentUserDep, uow: UowDep,
):
    _require_superadmin(current_user)
    return templates.TemplateResponse(
        request=request,
        name="mcp_connection_form.html",
        context={
            "request": request,
            "current_user": current_user,
            **await navigation_context(uow, current_user),
        },
    )


@router.post("/mcp")
async def create_mcp_connection(
    current_user: CurrentUserDep,
    uow: UowDep,
    name: str = Form(...),
    namespace: str = Form(...),
    endpoint: str = Form(...),
    auth_secret_ref: str = Form(default=""),
):
    _require_superadmin(current_user)
    now = dt.datetime.now(dt.timezone.utc)
    connection = McpConnection(
        id=f"mcp_{uuid.uuid4().hex}",
        name=name.strip(),
        namespace=namespace,
        transport="streamable_http",
        endpoint_or_command=endpoint,
        auth_secret_ref=auth_secret_ref.strip() or None,
        created_by=current_user["id"],
        created_at=now,
        updated_at=now,
    )
    if not connection.name:
        raise HTTPException(status_code=422, detail="MCP connection name is required")
    try:
        await McpConnectionService().create(connection, uow)
        await uow.commit()
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return RedirectResponse(f"/management/mcp/{connection.id}", status_code=303)


@router.get("/mcp/{connection_id}", response_class=HTMLResponse)
async def mcp_connection_page(
    request: Request,
    connection_id: str,
    current_user: CurrentUserDep,
    uow: UowDep,
):
    _require_superadmin(current_user)
    connection = await uow.mcp_connections.get(connection_id)
    if connection is None:
        raise HTTPException(status_code=404, detail="MCP connection not found")
    return templates.TemplateResponse(
        request=request,
        name="mcp_connection.html",
        context={
            "request": request,
            "current_user": current_user,
            "connection": connection,
            "tools": await uow.mcp_connections.list_discovered_tools(connection_id),
            "has_secret_ref": connection.auth_secret_ref is not None,
            **await navigation_context(uow, current_user),
        },
    )


@router.post("/mcp/{connection_id}/discover")
async def discover_mcp_connection_tools(
    connection_id: str,
    current_user: CurrentUserDep,
    uow: UowDep,
):
    _require_superadmin(current_user)
    connection = await uow.mcp_connections.get(connection_id)
    if connection is None:
        raise HTTPException(status_code=404, detail="MCP connection not found")
    try:
        client = await build_external_discovery_client(connection)
        await discover_mcp_tools(connection, uow, client=client)
        await uow.commit()
    except (KeyError, RuntimeError, ValueError) as exc:
        raise HTTPException(
            status_code=422,
            detail="MCP discovery failed. Check the server configuration and provider.",
        ) from exc
    return RedirectResponse(f"/management/mcp/{connection_id}", status_code=303)


@router.post("/mcp/{connection_id}/enabled")
async def update_mcp_connection_enabled(
    connection_id: str,
    current_user: CurrentUserDep,
    uow: UowDep,
    enabled: str = Form(...),
):
    _require_superadmin(current_user)
    normalized = enabled.strip().lower()
    if normalized not in {"true", "false"}:
        raise HTTPException(status_code=422, detail="Invalid MCP enabled state")
    try:
        await uow.mcp_connections.set_enabled(
            connection_id, normalized == "true"
        )
        await uow.commit()
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return RedirectResponse(f"/management/mcp/{connection_id}", status_code=303)


@router.post("/mcp/{connection_id}/tools/{tool_name}/approval")
async def update_mcp_tool_approval(
    connection_id: str,
    tool_name: str,
    current_user: CurrentUserDep,
    uow: UowDep,
    state: str = Form(...),
):
    _require_superadmin(current_user)
    if state not in {"approved", "pending", "disabled"}:
        raise HTTPException(status_code=422, detail="Invalid MCP tool approval state")
    try:
        await uow.mcp_connections.set_tool_approval_state(
            connection_id, tool_name, state
        )
        await uow.commit()
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return RedirectResponse(f"/management/mcp/{connection_id}", status_code=303)


@router.get("/tools/runs", response_class=HTMLResponse)
async def agent_tool_runs_page(
    request: Request, current_user: CurrentUserDep, uow: UowDep
):
    if current_user["role"] != "superadmin":
        raise HTTPException(status_code=403, detail="Only superadmins can view tool history")
    calls = await uow.agent_tool_calls.list_recent(limit=200)
    return templates.TemplateResponse(
        request=request,
        name="agent_tool_runs.html",
        context={
            "request": request,
            "current_user": current_user,
            "calls": calls,
            **await navigation_context(uow, current_user),
        },
    )


@router.get("/agents/{agent_id}/settings", response_class=HTMLResponse)
async def agent_settings_page(
    request: Request, agent_id: str, current_user: CurrentUserDep, uow: UowDep
):
    service = AgentToolPolicyService(build_registry())
    try:
        policy = await service.view(current_user, agent_id, uow)
    except PermissionError as exc:
        raise _forbidden(exc) from exc
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return templates.TemplateResponse(
        request=request,
        name="agent_settings.html",
        context={
            "request": request,
            "current_user": current_user,
            "cancel_url": cancel_url(request, "/management"),
            **policy,
            **await navigation_context(uow, current_user),
        },
    )


@router.post("/agents/{agent_id}/tools")
async def update_agent_tools(
    agent_id: str,
    current_user: CurrentUserDep,
    uow: UowDep,
    tool_names: list[str] = Form(default=[]),
    mcp_tool_names: list[str] = Form(default=[]),
):
    service = AgentToolPolicyService(build_registry())
    try:
        await service.replace_tool_access(
            current_user, agent_id, set(tool_names), set(mcp_tool_names), uow
        )
    except PermissionError as exc:
        raise _forbidden(exc) from exc
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return RedirectResponse(
        f"/management/agents/{agent_id}/settings", status_code=303
    )
