from pathlib import Path

from crewspace.application.tools import build_registry, native_tool_presets


def test_agent_tool_migration_uses_cross_dialect_parameters():
    migration = Path(
        "migrations/versions/20260820_03_agent_tool_permissions.py"
    ).read_text()
    assert "exec_driver_sql" not in migration
    assert "text(" in migration
    assert "ck_agent_tool_provider_type" in migration
    assert "ck_agent_tool_approval_mode" in migration


def test_migration_preserves_tools_for_every_existing_builtin_agent(tmp_path):
    import sqlite3

    from alembic import command
    from alembic.config import Config

    db_path = tmp_path / "legacy-agent.db"
    config = Config("alembic.ini")
    config.set_main_option(
        "sqlalchemy.url", f"sqlite+aiosqlite:///{db_path}"
    )
    command.upgrade(config, "20260820_02")

    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "INSERT INTO member "
            "(id, kind, name, role, backend, uses_app_llm) "
            "VALUES ('agent_legacy', 'agent', 'Legacy', 'agent', 'llm', 1)"
        )
        conn.commit()

    command.upgrade(config, "head")
    with sqlite3.connect(db_path) as conn:
        count = conn.execute(
            "SELECT COUNT(*) FROM agent_tool_permission "
            "WHERE agent_id='agent_legacy' AND enabled=1"
        ).fetchone()[0]
    assert count == len(build_registry().list_tools())


def test_native_tools_declare_policy_metadata():
    tools = build_registry().list_tools()

    assert tools
    for tool in tools:
        assert tool.provider == "crewspace"
        assert tool.category in {"boards", "chat", "scheduling"}
        assert tool.mutability in {"read", "write"}
        assert tool.risk in {"low", "medium", "high"}


def test_native_tool_presets_are_derived_from_metadata():
    registry = build_registry()
    presets = native_tool_presets(registry.list_tools())
    all_names = {tool.name for tool in registry.list_tools()}
    read_names = {
        tool.name for tool in registry.list_tools() if tool.mutability == "read"
    }

    assert presets["none"] == set()
    assert presets["read_only"] == read_names
    assert presets["standard"] == {
        tool.name for tool in registry.list_tools() if tool.risk != "high"
    }
    assert presets["all"] == all_names
    assert "create_cronjob" not in presets["standard"]


async def test_agent_tool_permissions_can_be_replaced_and_listed(app):
    async with app.state.db.uow() as uow:
        await uow.agent_policies.replace_native_tools(
            "agent_crewspace", {"list_boards", "find_card"}
        )
        assert await uow.agent_policies.list_enabled_native_tools(
            "agent_crewspace"
        ) == {"list_boards", "find_card"}

        await uow.agent_policies.replace_native_tools(
            "agent_crewspace", {"post_message"}
        )
        assert await uow.agent_policies.list_enabled_native_tools(
            "agent_crewspace"
        ) == {"post_message"}


async def test_new_builtin_agent_defaults_to_no_tools(client, app):
    response = client.post(
        "/auth/agents/register",
        data={"name": "Restricted", "avatar": "🤖", "uses_app_llm": "1"},
    )
    assert response.status_code == 200

    async with app.state.db.uow() as uow:
        agent = await uow.auth.get_member_by_name("Restricted")
        assert await uow.agent_policies.list_enabled_native_tools(agent["id"]) == set()


async def test_bound_runner_rejects_tools_outside_agent_allowlist(app):
    from crewspace.application.tools import ToolPermissionDenied

    async with app.state.db.uow() as uow:
        runner = build_registry().bind(
            uow,
            principal_id="user_bilal",
            agent_id="agent_crewspace",
            allowed_tools={"list_boards"},
        )
        assert await runner.run("list_boards")
        try:
            await runner.run("create_card", column_id="col_todo", title="Blocked")
        except ToolPermissionDenied as exc:
            assert "not allowed" in str(exc)
        else:
            raise AssertionError("disabled tool must not execute")
        assert await uow.boards.find_card_by_title("board_main", "Blocked") is None


async def test_card_event_callbacks_respect_each_builtin_agent_allowlist(app):
    from crewspace.application.services import BoardService
    from crewspace.config import Settings

    async with app.state.db.uow() as uow:
        for agent_id in ("agent_crewspace", "agent_planner"):
            await uow.agent_policies.replace_native_tools(agent_id, {"list_boards"})
        service = BoardService(build_registry(), Settings())
        card = await service.create_card(
            "col_todo", "No automatic comment", uow, actor_id="user_bilal"
        )
        refreshed = await uow.boards.get_card(card.id)
        assert refreshed is not None
        assert refreshed.comments == []


async def test_agent_tool_authorship_is_separate_from_human_resource_principal(app):
    from crewspace.application.services import BoardService
    from crewspace.config import Settings

    async with app.state.db.uow() as uow:
        await uow.agent_policies.replace_native_tools("agent_crewspace", set())
        await uow.agent_policies.replace_native_tools(
            "agent_planner", {"comment_card"}
        )
        service = BoardService(build_registry(), Settings())
        card = await service.create_card(
            "col_todo", "Agent-authored note", uow, actor_id="user_bilal"
        )
        refreshed = await uow.boards.get_card(card.id)
        assert refreshed is not None
        assert len(refreshed.comments) == 1
        assert refreshed.comments[0].author_id == "agent_planner"
        assert refreshed.comments[0].author_id != "user_bilal"


async def test_agent_registry_filters_builtin_llm_discovery(app):
    from crewspace.config import Settings
    from crewspace.infrastructure.agents.registry import AgentRegistry

    async with app.state.db.uow() as uow:
        await uow.agent_policies.replace_native_tools(
            "agent_crewspace", {"list_boards"}
        )
        provider = await AgentRegistry.build(Settings(), uow)
        crewspace = provider._local["agent_crewspace"]
        assert {tool.name for tool in crewspace._tools} == {"list_boards"}


def test_superadmin_can_manage_builtin_agent_tools(client, app):
    management = client.get("/management")
    assert 'href="/management/agents/agent_crewspace/settings"' in management.text

    page = client.get("/management/agents/agent_crewspace/settings")
    assert page.status_code == 200
    assert "Agent settings" in page.text
    assert "Boards" in page.text
    assert 'name="tool_names" value="list_boards"' in page.text
    assert 'value="list_boards" checked' in page.text
    assert "Standard collaborator" in page.text

    updated = client.post(
        "/management/agents/agent_crewspace/tools",
        data={"tool_names": ["list_boards", "find_card"]},
        follow_redirects=False,
    )
    assert updated.status_code == 303
    assert updated.headers["location"] == "/management/agents/agent_crewspace/settings"

    import asyncio

    async def enabled():
        async with app.state.db.uow() as uow:
            return await uow.agent_policies.list_enabled_native_tools(
                "agent_crewspace"
            )

    assert asyncio.run(enabled()) == {"list_boards", "find_card"}


def test_agent_tool_settings_reject_unknown_tools(client):
    response = client.post(
        "/management/agents/agent_crewspace/tools",
        data={"tool_names": ["not_a_real_tool"]},
    )
    assert response.status_code == 422


def test_non_superadmin_cannot_manage_agent_tools(client, app):
    import asyncio

    async def demote():
        async with app.state.db.uow() as uow:
            await uow._conn.execute(
                "UPDATE member SET role='team_member' WHERE id='user_bilal'"
            )

    asyncio.run(demote())
    assert client.get(
        "/management/agents/agent_crewspace/settings"
    ).status_code == 403
    assert client.post(
        "/management/agents/agent_crewspace/tools",
        data={"tool_names": ["list_boards"]},
    ).status_code == 403


async def test_disabled_seeded_agent_tools_remain_disabled_after_restart(app):
    from crewspace.infrastructure.db import Database

    async with app.state.db.uow() as uow:
        await uow.agent_policies.replace_native_tools(
            "agent_crewspace", {"list_boards"}
        )

    restarted = await Database.create(app.state.settings)
    try:
        async with restarted.uow() as uow:
            assert await uow.agent_policies.list_enabled_native_tools(
                "agent_crewspace"
            ) == {"list_boards"}
    finally:
        await restarted.close()


async def test_seeded_builtin_agents_keep_compatibility_tools(app):
    expected = {tool.name for tool in build_registry().list_tools()}
    async with app.state.db.uow() as uow:
        assert await uow.agent_policies.list_enabled_native_tools(
            "agent_crewspace"
        ) == expected
        assert await uow.agent_policies.list_enabled_native_tools(
            "agent_planner"
        ) == expected


async def test_agent_get_card_returns_metadata_and_respects_scope(app):
    from crewspace.application.tools import ToolPermissionDenied

    async with app.state.db.uow() as uow:
        runner = build_registry().bind(
            uow,
            principal_id="user_bilal",
            agent_id="agent_crewspace",
            allowed_tools={"get_card"},
        )
        # Create a card directly for a deterministic id.
        card = await uow.boards.add_card("col_todo", "Tool card", description="desc")
        result = await runner.run("get_card", card_id=card.id)
        assert result is not None
        assert result["id"] == card.id
        assert result["description"] == "desc"
        assert result["priority"] is None
        assert "labels" in result
        # Disallowed from the allowlist -> ToolPermissionDenied
        denied = build_registry().bind(
            uow,
            principal_id="user_bilal",
            agent_id="agent_crewspace",
            allowed_tools={"list_boards"},
        )
        try:
            await denied.run("get_card", card_id=card.id)
        except ToolPermissionDenied as exc:
            assert "not allowed" in str(exc)
        else:
            raise AssertionError("disabled get_card must not execute")


async def test_agent_update_card_persists_and_clears(app):
    from crewspace.application.tools import ToolPermissionDenied

    async with app.state.db.uow() as uow:
        runner = build_registry().bind(
            uow,
            principal_id="user_bilal",
            agent_id="agent_crewspace",
            allowed_tools={"update_card"},
        )
        card = await uow.boards.add_card("col_todo", "Update me", description="old")
        result = await runner.run(
            "update_card",
            card_id=card.id,
            description="new description",
            due_date="2026-10-01",
            priority="high",
            labels=["backend", "auth"],
        )
        assert result["description"] == "new description"
        assert result["due_date"] == "2026-10-01"
        assert result["priority"] == "high"
        assert set(result["labels"]) == {"backend", "auth"}

        # Empty string clears optional fields.
        cleared = await runner.run(
            "update_card", card_id=card.id, description="", due_date="", priority=""
        )
        assert cleared["description"] is None
        assert cleared["due_date"] is None
        assert cleared["priority"] is None

        # Invalid priority is rejected without persisting.
        try:
            await runner.run("update_card", card_id=card.id, priority="bogus")
        except ValueError:
            pass
        else:
            raise AssertionError("invalid priority must raise ValueError")

        # Policy deny still blocks the mutation.
        denied = build_registry().bind(
            uow,
            principal_id="user_bilal",
            agent_id="agent_crewspace",
            allowed_tools={"get_card"},
        )
        try:
            await denied.run("update_card", card_id=card.id, title="Blocked edit")
        except ToolPermissionDenied as exc:
            assert "not allowed" in str(exc)
        else:
            raise AssertionError("disabled update_card must not execute")
