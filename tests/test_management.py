"""End-to-end management UI and authorization behavior."""
from __future__ import annotations

import asyncio


def test_superadmin_sees_management_navigation(client):
    response = client.get("/")
    assert response.status_code == 200
    assert "Crewspace" in response.text
    assert "Humans and agents, working together" in response.text
    assert "Agentic Kanban" not in response.text
    assert 'class="user-menu"' in response.text
    assert 'href="/management"' in response.text
    assert "superadmin" in response.text


def test_sidebar_separates_workspaces_and_channels(client):
    response = client.get("/")
    assert response.status_code == 200
    assert 'class="workspace-nav"' in response.text
    assert "Acme OS" in response.text
    assert 'href="/channels/chan_general"' in response.text
    assert "# general" in response.text
    assert 'aria-label="Workspace actions"' in response.text
    assert 'aria-label="Channel actions"' in response.text
    assert 'aria-label="Workspaces and channels actions"' in response.text
    assert '>Manage workspaces<' in response.text
    assert 'href="/management/workspaces/ws_default/channels/new"' in response.text
    assert response.text.count('href="/management/workspaces/ws_default"') == 1
    assert 'href="/management/channels/chan_general"' in response.text
    assert 'href="/management/channels/chan_general/members"' in response.text


def test_agents_section_owns_register_action(client):
    response = client.get("/")
    assert response.status_code == 200
    assert 'aria-label="Agents actions"' in response.text
    assert '>Register agent<' in response.text
    assert response.text.count('href="/auth/agents/register"') == 2


def test_channel_actions_open_distinct_forms(client):
    manage = client.get("/management/channels/chan_general")
    assert manage.status_code == 200
    assert "Manage channel" in manage.text
    assert 'action="/management/channels/chan_general"' in manage.text
    assert "Channel name" in manage.text
    assert "Who can be mentioned" in manage.text
    assert "Assign member or agent" not in manage.text

    members = client.get("/management/channels/chan_general/members")
    assert members.status_code == 200
    assert "Manage members" in members.text
    assert 'action="/management/channels/chan_general/members"' in members.text
    assert "Assign member or agent" in members.text
    assert "Channel name" not in members.text


def test_team_leader_can_update_channel_settings(client):
    response = client.post(
        "/management/channels/chan_general",
        data={
            "name": "announcements",
            "topic": "Company updates",
            "channel_type": "permanent",
            "mention_policy": "all_team",
        },
        follow_redirects=True,
    )
    assert response.status_code == 200
    assert "announcements" in response.text
    assert "Company updates" in response.text


def test_workspace_actions_open_distinct_forms(client):
    manage = client.get("/management/workspaces/ws_default")
    assert manage.status_code == 200
    assert "Manage workspace" in manage.text
    assert 'action="/management/workspaces/ws_default"' in manage.text
    assert "Workspace name" in manage.text
    assert "Channel name" not in manage.text

    add_channel = client.get("/management/workspaces/ws_default/channels/new")
    assert add_channel.status_code == 200
    assert "Add channel" in add_channel.text
    assert 'action="/management/workspaces/ws_default/channels"' in add_channel.text
    assert "Channel name" in add_channel.text


def test_manage_workspaces_opens_workspace_only_page(client):
    response = client.get("/management/workspaces")
    assert response.status_code == 200
    assert "Manage workspaces" in response.text
    assert "Acme OS" in response.text
    assert 'href="/management/workspaces/ws_default"' in response.text
    assert 'href="/management/workspaces/ws_default/channels/new"' in response.text
    assert "Team management" not in response.text
    assert "Create team" not in response.text


def test_team_leader_can_rename_workspace(client):
    response = client.post(
        "/management/workspaces/ws_default",
        data={"name": "Acme Platform"},
        follow_redirects=True,
    )
    assert response.status_code == 200
    assert "Acme Platform" in response.text


def test_register_agent_has_cancel_button(client):
    response = client.get("/auth/agents/register")
    assert response.status_code == 200
    assert 'class="sidebar"' in response.text
    assert "Workspaces &amp; channels" in response.text or "Workspaces & channels" in response.text
    assert "Acme OS" in response.text
    assert 'href="/management"' in response.text
    assert ">Cancel<" in response.text


def test_add_human_uses_dedicated_form_page(client):
    home = client.get("/")
    assert 'href="/management/humans/new"' in home.text
    assert 'href="/management#add-human"' not in home.text

    response = client.get("/management/humans/new")
    assert response.status_code == 200
    assert 'class="sidebar"' in response.text
    assert "Add human" in response.text
    assert 'action="/management/humans"' in response.text
    assert "Human name" in response.text
    assert "Temporary password" in response.text
    assert 'href="/management"' in response.text
    assert "Team management" not in response.text
    assert "Create team" not in response.text


def test_action_menus_close_when_clicking_outside(client):
    response = client.get("/")
    assert response.status_code == 200
    assert "document.addEventListener(\"click\"" in response.text
    assert "details[open].action-menu" in response.text


def test_management_dashboard_shows_team_hierarchy(client):
    response = client.get("/management")
    assert response.status_code == 200
    assert "Team management" in response.text
    assert "Acme Corp" in response.text
    assert "Acme OS" in response.text
    assert "general" in response.text
    assert "Planner" in response.text


def test_superadmin_can_create_team_and_assign_leader(client):
    response = client.post(
        "/management/teams",
        data={"name": "Platform", "leader_id": "user_bilal"},
        follow_redirects=True,
    )
    assert response.status_code == 200
    assert "Platform" in response.text
    assert "Bilal" in response.text
    assert "Team leader" in response.text


def test_superadmin_can_add_human_from_management(client):
    response = client.post(
        "/management/humans",
        data={"name": "Aisha", "password": "temporary-password", "team_id": "team_acme"},
        follow_redirects=True,
    )
    assert response.status_code == 200
    assert "Aisha" in response.text
    assert "Team member" in response.text


def test_superadmin_add_human_form_requires_team_selection(client):
    response = client.get("/management/humans/new")
    assert response.status_code == 200
    assert "Choose team" in response.text
    assert 'option value="team_acme"' in response.text


def test_team_leader_adds_human_to_own_team(client, app):
    import asyncio

    async def become_leader_only():
        async with app.state.db.uow() as uow:
            await uow._conn.execute(
                "UPDATE member SET role = 'team_member' WHERE id = 'user_bilal'"
            )

    asyncio.run(become_leader_only())
    form = client.get("/management/humans/new")
    assert form.status_code == 200
    assert "Will be added to Acme Corp" in form.text
    assert "Choose team" not in form.text
    response = client.post(
        "/management/humans",
        data={"name": "Leader Hire", "password": "temporary-password"},
    )
    assert response.status_code == 200


def test_engineering_manager_selects_one_of_managed_teams(client, app):
    import asyncio

    async def become_manager():
        async with app.state.db.uow() as uow:
            await uow._conn.execute(
                "UPDATE member SET role = 'engineering_manager' WHERE id = 'user_bilal'"
            )

    asyncio.run(become_manager())
    form = client.get("/management/humans/new")
    assert form.status_code == 200
    assert "Choose team" in form.text
    assert "Acme Corp" in form.text
    response = client.post(
        "/management/humans",
        data={"name": "Manager Hire", "password": "temporary-password", "team_id": "team_acme"},
    )
    assert response.status_code == 200


def test_engineering_manager_cannot_add_human_to_unmanaged_team(client, app):
    import asyncio

    async def become_manager_and_add_other_team():
        async with app.state.db.uow() as uow:
            await uow._conn.execute(
                "UPDATE member SET role = 'engineering_manager' WHERE id = 'user_bilal'"
            )
            await uow._conn.execute(
                "INSERT INTO team (id,name,created_by,created_at) VALUES ('team_other','Other','user_bilal','2026-01-01T00:00:00+00:00')"
            )

    asyncio.run(become_manager_and_add_other_team())
    response = client.post(
        "/management/humans",
        data={"name": "Forbidden Hire", "password": "temporary-password", "team_id": "team_other"},
    )
    assert response.status_code == 403


def test_channel_navigation_opens_selected_channel(client):
    response = client.get("/channels/chan_general")
    assert response.status_code == 200
    assert "#general — Acme OS" in response.text
    assert 'const channel = "chan_general"' in response.text


def test_team_leader_can_create_workspace_and_channel(client):
    workspace = client.post(
        "/management/teams/team_acme/workspaces",
        data={"name": "Launch"},
        follow_redirects=True,
    )
    assert workspace.status_code == 200
    assert "Launch" in workspace.text

    workspace_id = workspace.headers.get("x-workspace-id")
    assert workspace_id
    channel = client.post(
        f"/management/workspaces/{workspace_id}/channels",
        data={
            "name": "war-room",
            "topic": "Launch coordination",
            "channel_type": "temporary",
            "mention_policy": "specific",
        },
        follow_redirects=True,
    )
    assert channel.status_code == 200
    assert "war-room" in channel.text
    assert "Temporary" in channel.text
    assert "Specific channel members" in channel.text


def test_team_leader_can_assign_human_and_agent_to_channel(client):
    response = client.post(
        "/management/channels/chan_general/members",
        data={"member_id": "agent_planner"},
        follow_redirects=True,
    )
    assert response.status_code == 200
    assert "Planner" in response.text
    assert "Agent" in response.text


def test_team_member_cannot_create_workspace(client, app):
    # Change the seeded user from leader to ordinary team member through the
    # real database, then exercise the HTTP authorization boundary.
    import asyncio

    async def demote():
        async with app.state.db.uow() as uow:
            await uow._conn.execute(
                "UPDATE team_member SET role = 'member' WHERE team_id = 'team_acme' AND member_id = 'user_bilal'"
            )
            await uow._conn.execute(
                "UPDATE member SET role = 'team_member' WHERE id = 'user_bilal'"
            )

    asyncio.run(demote())
    response = client.post(
        "/management/teams/team_acme/workspaces",
        data={"name": "Forbidden"},
        follow_redirects=False,
    )
    assert response.status_code == 403


def test_team_member_sees_no_management_actions_and_direct_requests_are_forbidden(client, app):
    import asyncio

    async def demote():
        async with app.state.db.uow() as uow:
            await uow._conn.execute(
                "UPDATE team_member SET role='member' WHERE team_id='team_acme' AND member_id='user_bilal'"
            )
            await uow._conn.execute(
                "UPDATE member SET role='team_member' WHERE id='user_bilal'"
            )

    asyncio.run(demote())
    home = client.get("/")
    assert 'href="/management"' not in home.text
    assert 'aria-label="Workspace actions"' not in home.text
    assert 'aria-label="Channel actions"' not in home.text
    assert 'aria-label="Agents actions"' in home.text
    assert 'href="/auth/agents/register"' in home.text
    assert "Add human" not in home.text
    assert client.get("/management").status_code == 403
    assert client.get("/management/workspaces").status_code == 403
    assert client.get("/management/workspaces/ws_default").status_code == 403
    assert client.get("/management/channels/chan_general").status_code == 403
    assert client.get("/management/channels/chan_general/members").status_code == 403
    assert client.get("/management/humans/new").status_code == 403
    # Any logged-in user may register a remote (WebSocket) agent; the superadmin-only
    # path is creating a builtin agent that uses the main app LLM.
    assert client.get("/auth/agents/register").status_code == 200
    assert client.post("/auth/agents/register", data={"name": "Forbidden"}).status_code == 200
    assert client.post(
        "/auth/agents/register", data={"name": "Forbidden", "uses_app_llm": "1"}
    ).status_code == 403
    assert client.post("/management/humans", data={"name": "No", "password": "password123", "team_id": "team_acme"}).status_code == 403
    assert client.post("/management/teams", data={"name": "No", "leader_id": "user_bilal"}).status_code == 403
    assert client.post("/management/teams/team_acme/members", data={"member_id": "agent_planner"}).status_code == 403
    assert client.post("/management/workspaces/ws_default", data={"name": "No"}).status_code == 403
    assert client.post("/management/workspaces/ws_default/channels", data={"name": "no"}).status_code == 403
    assert client.post("/management/channels/chan_general", data={"name": "no"}).status_code == 403
    assert client.post("/management/channels/chan_general/members", data={"member_id": "agent_planner"}).status_code == 403
    assert client.post("/management/channels/chan_general/members/agent_planner/remove").status_code == 403


def test_engineering_manager_can_manage_assigned_team_but_not_other_team(client, app):
    import asyncio

    async def arrange():
        async with app.state.db.uow() as uow:
            await uow._conn.execute("UPDATE member SET role='engineering_manager' WHERE id='user_bilal'")
            await uow._conn.execute("UPDATE team_member SET role='member' WHERE team_id='team_acme' AND member_id='user_bilal'")
            await uow._conn.execute("INSERT INTO team(id,name,created_by,created_at) VALUES('team_other','Other','user_bilal','2026-01-01T00:00:00+00:00')")
            await uow._conn.execute("INSERT INTO workspace(id,team_id,name,created_by,created_at) VALUES('ws_other','team_other','Other WS','user_bilal','2026-01-01T00:00:00+00:00')")
            await uow._conn.execute("INSERT INTO channel(id,workspace_id,name,channel_type,mention_policy,created_by,created_at) VALUES('chan_other','ws_other','other','permanent','channel_members','user_bilal','2026-01-01T00:00:00+00:00')")

    asyncio.run(arrange())
    home = client.get("/")
    assert 'href="/management"' in home.text
    assert 'aria-label="Workspace actions"' in home.text
    assert 'aria-label="Channel actions"' in home.text
    assert "Add human" in home.text
    assert 'aria-label="Agents actions"' in home.text
    assert 'href="/auth/agents/register"' in home.text
    assert client.get("/management/workspaces/ws_default").status_code == 200
    assert client.get("/management/channels/chan_general").status_code == 200
    assert client.post("/management/teams/team_acme/workspaces", data={"name": "Managed"}).status_code == 200
    assert client.get("/management/workspaces/ws_other").status_code == 403
    assert client.get("/management/channels/chan_other").status_code == 403
    assert client.post("/management/teams/team_other/workspaces", data={"name": "Forbidden"}).status_code == 403
    assert client.post("/management/teams", data={"name": "Forbidden", "leader_id": "user_bilal"}).status_code == 403
    # Any logged-in user (incl. engineering manager) may register a remote agent.
    assert client.get("/auth/agents/register").status_code == 200
    assert client.post(
        "/auth/agents/register", data={"name": "Forbidden", "uses_app_llm": "1"}
    ).status_code == 403


def test_manager_can_archive_and_restore_channel(client):
    archived = client.post(
        "/management/channels/chan_general/archive", follow_redirects=False
    )
    assert archived.status_code == 303
    assert client.get("/channels/chan_general").status_code == 404
    assert 'href="/channels/chan_general"' not in client.get("/").text

    restored = client.post(
        "/management/channels/chan_general/restore", follow_redirects=False
    )
    assert restored.status_code == 303
    assert client.get("/channels/chan_general").status_code == 200


def test_archived_workspace_and_team_leave_active_navigation(client):
    assert client.post("/management/workspaces/ws_default/archive").status_code == 200
    assert "Acme OS" not in client.get("/").text
    assert client.post("/management/workspaces/ws_default/restore").status_code == 200
    assert "Acme OS" in client.get("/").text

    assert client.post("/management/teams/team_acme/archive").status_code == 200
    assert client.get("/").status_code == 404
    assert client.post("/management/teams/team_acme/restore").status_code == 200


def test_agent_archive_preserves_history_and_removes_agent_targets(client):
    before = client.get("/channels/chan_general/messages").json()
    assert any(message["author_id"] == "agent_planner" for message in before)
    assert client.post("/management/agents/agent_planner/archive").status_code == 200
    assert 'href="/direct/agent_planner"' not in client.get("/").text
    after = client.get("/channels/chan_general/messages").json()
    assert any(message["author_id"] == "agent_planner" for message in after)
    assert client.post("/management/agents/agent_planner/restore").status_code == 200


def test_permanent_delete_is_post_only_superadmin_and_requires_exact_name(client, app):
    confirmation = client.get("/management/channels/chan_general/delete")
    assert confirmation.status_code == 200
    assert "Permanently delete channel" in confirmation.text
    assert "messages" in confirmation.text
    assert 'name="confirmation"' in confirmation.text
    assert client.get("/management/channels/chan_general/delete/confirm").status_code == 404
    assert client.post(
        "/management/channels/chan_general/delete", data={"confirmation": "wrong"}
    ).status_code == 422

    import asyncio

    async def demote():
        async with app.state.db.uow() as uow:
            await uow._conn.execute(
                "UPDATE member SET role='engineering_manager' WHERE id='user_bilal'"
            )

    asyncio.run(demote())
    assert client.get("/management/channels/chan_general/delete").status_code == 403
    assert client.post(
        "/management/channels/chan_general/delete", data={"confirmation": "general"}
    ).status_code == 403


def test_superadmin_can_permanently_delete_channel_with_dependents(client):
    response = client.post(
        "/management/channels/chan_general/delete",
        data={"confirmation": "general"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert client.get("/channels/chan_general").status_code == 404
    assert client.get("/channels/chan_general/messages").status_code == 404


def test_lifecycle_controls_are_post_forms_and_delete_has_dedicated_page(client):
    channel = client.get("/management/channels/chan_general")
    assert 'action="/management/channels/chan_general/archive"' in channel.text
    assert 'href="/management/channels/chan_general/delete"' in channel.text
    workspace = client.get("/management/workspaces/ws_default")
    assert 'action="/management/workspaces/ws_default/archive"' in workspace.text


def test_superadmin_can_delete_workspace_cascade(client):
    response = client.post(
        "/management/workspaces/ws_default/delete",
        data={"confirmation": "Acme OS"}, follow_redirects=False,
    )
    assert response.status_code == 303
    assert client.get("/channels/chan_general").status_code == 404


def test_superadmin_can_delete_team_cascade(client):
    response = client.post(
        "/management/teams/team_acme/delete",
        data={"confirmation": "Acme Corp"}, follow_redirects=False,
    )
    assert response.status_code == 303
    assert client.get("/").status_code == 404
    assert client.get("/management/teams/team_acme/delete").status_code == 404


def test_agent_delete_preview_discloses_creator_owned_hierarchy(client, app):
    import asyncio

    async def arrange():
        async with app.state.db.uow() as uow:
            now = "2026-01-01T00:00:00+00:00"
            await uow._conn.execute(
                "INSERT INTO team(id,name,created_by,created_at) VALUES(?,?,?,?)",
                ("team_agent_owned", "Agent owned", "agent_planner", now),
            )
            await uow._conn.execute(
                "INSERT INTO workspace(id,team_id,name,created_by,created_at) VALUES(?,?,?,?,?)",
                ("ws_agent_owned", "team_agent_owned", "Agent workspace", "agent_planner", now),
            )
            await uow._conn.execute(
                "INSERT INTO channel(id,workspace_id,name,channel_type,mention_policy,created_by,created_at) VALUES(?,?,?,?,?,?,?)",
                (
                    "chan_agent_owned", "ws_agent_owned", "agent-owned", "permanent",
                    "channel_members", "agent_planner", now,
                ),
            )

    asyncio.run(arrange())
    response = client.get("/management/agents/agent_planner/delete")

    assert response.status_code == 200
    assert "creator-owned teams" in response.text
    assert "creator-owned workspaces" in response.text
    assert "creator-owned channels" in response.text


def test_deleting_agent_removes_replies_to_agent_thread_roots(client, app):
    import asyncio

    async def arrange():
        async with app.state.db.uow() as uow:
            root = await uow.chat.add_message(
                "chan_general", "agent_planner", "Agent-owned root"
            )
            reply = await uow.chat.add_message(
                "chan_general", "user_bilal", "Human reply", root.id
            )
            return root.id, reply.id

    root_id, reply_id = asyncio.run(arrange())
    response = client.post(
        "/management/agents/agent_planner/delete",
        data={"confirmation": "Planner"}, follow_redirects=False,
    )
    assert response.status_code == 303

    async def remaining_ids():
        async with app.state.db.uow() as uow:
            rows = await (
                await uow._conn.execute(
                    "SELECT id FROM message WHERE id IN (?,?)", (root_id, reply_id)
                )
            ).fetchall()
            return {row["id"] for row in rows}

    assert asyncio.run(remaining_ids()) == set()


def test_superadmin_can_delete_agent_and_keep_cards(client):
    response = client.post(
        "/management/agents/agent_planner/delete",
        data={"confirmation": "Planner"}, follow_redirects=False,
    )
    assert response.status_code == 303
    assert 'href="/direct/agent_planner"' not in client.get("/").text
    assert client.get("/board/board_main").status_code == 200


def test_superadmin_can_create_builtin_app_llm_agent(client, app):
    response = client.post(
        "/auth/agents/register",
        data={"name": "Researcher", "avatar": "🔬", "backend": "stub", "uses_app_llm": "1"},
        follow_redirects=False,
    )
    assert response.status_code == 200
    assert "builtin agent" in response.text

    async def fetch():
        async with app.state.db.uow() as uow:
            return await uow.auth.get_member("agent_researcher")

    member = asyncio.run(fetch())
    assert member is not None
    assert member["kind"] == "agent"
    assert member["backend"] == "llm"
    assert member["uses_app_llm"] == 1
    # No keypair: a builtin agent never connects over WebSocket.
    assert member["pubkey"] is None


def test_non_superadmin_cannot_create_builtin_app_llm_agent(client, app):
    # Superadmin (the `client` fixture) creates a plain team member via the UI.
    created = client.post(
        "/management/humans",
        data={"name": "Intern", "password": "intern123", "team_id": "team_acme"},
    )
    assert created.status_code == 200

    # Switch the same test client from the superadmin session to the new member.
    assert client.post("/auth/logout").status_code == 200
    login = client.post(
        "/auth/login",
        data={"username": "Intern", "password": "intern123"},
        follow_redirects=False,
    )
    assert login.status_code == 303
    # A non-superadmin may register a remote (WebSocket) agent...
    ok = client.post("/auth/agents/register", data={"name": "Sidekick"})
    assert ok.status_code == 200
    # ...but not a builtin app-LLM agent.
    blocked = client.post(
        "/auth/agents/register", data={"name": "Cheater", "uses_app_llm": "1"}
    )
    assert blocked.status_code == 403
