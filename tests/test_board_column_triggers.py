"""M7.5 — move-to-column workflow triggers (RED first).

Core tracer: configure a column→workflow rule, move a card into the trigger
column through the real BoardService, and assert a `column_move` workflow run is
enqueued for the moved card. Duplicate/retried moves must NOT double-enqueue
(idempotent trigger key). Moves to a column with no rule, a disabled rule, or a
misconfigured (missing) workflow never enqueue and the card still moves normally.
"""
from __future__ import annotations


def _rule_payload(**overrides):
    payload = {
        "name": "col_trigger_flow",
        "description": "Triggered by board column move",
        "channel_id": "chan_general",
        "enabled": True,
        "trigger_type": "column_move",
        "trigger_config": {},
        "steps": [
            {
                "id": "step_1",
                "name": "Delay",
                "action": "delay",
                "timeout_seconds": 5,
                "config": {"seconds": "0"},
            }
        ],
    }
    payload.update(overrides)
    return payload


async def _seed_workflow(app, run_id_hint: str | None = None) -> str:
    from crewspace.application.workflows import WorkflowService

    async with app.state.db.uow() as uow:
        svc = WorkflowService()
        created = await svc.create(
            uow, creator_id="user_bilal", data=_rule_payload()
        )
        return created.id


async def test_move_to_trigger_column_enqueues_column_move_run(app):
    """Moving a card into a configured trigger column creates a run."""
    from crewspace.application.services import BoardService
    from crewspace.application.tools import build_registry

    workflow_id = await _seed_workflow(app)
    service = BoardService(build_registry(), app.state.settings)

    async with app.state.db.uow() as uow:
        card = await uow.boards.add_card("col_todo", "Trigger me", actor_id="user_bilal")
        from crewspace.domain.entities import ColumnWorkflowRule
        await uow.boards.set_column_workflow(
            ColumnWorkflowRule(
                id="rule_1",
                board_id="board_main",
                column_id="col_done",
                workflow_id=workflow_id,
                enabled=True,
                changed_by="user_bilal",
            )
        )
        await uow.commit()

    async with app.state.db.uow() as uow:
        old_col, moved = await service.move_card(
            card.id, "col_done", uow, actor_id="user_bilal"
        )
        await uow.commit()
        assert moved is not None and moved.column_id == "col_done"

    async with app.state.db.uow() as uow:
        runs = await uow.workflows.list_runs(workflow_id)
    assert len(runs) == 1
    run = runs[0]
    assert run.trigger_type == "column_move"
    assert run.event.get("card_id") == card.id
    assert run.event.get("column_id") == "col_done"


async def test_duplicate_move_does_not_double_enqueue(app):
    """Repeating the same card→column move must not create a second run."""
    from crewspace.application.services import BoardService
    from crewspace.application.workflows import WorkflowService
    from crewspace.application.tools import build_registry

    workflow_id = await _seed_workflow(app)
    service = BoardService(build_registry(), app.state.settings)

    async with app.state.db.uow() as uow:
        card = await uow.boards.add_card("col_todo", "Once only", actor_id="user_bilal")
        from crewspace.domain.entities import ColumnWorkflowRule
        await uow.boards.set_column_workflow(
            ColumnWorkflowRule(
                id="rule_2", board_id="board_main", column_id="col_done",
                workflow_id=workflow_id, enabled=True, changed_by="user_bilal",
            )
        )
        await uow.commit()

    # First move -> enqueues.
    async with app.state.db.uow() as uow:
        await service.move_card(card.id, "col_done", uow, actor_id="user_bilal")
        await uow.commit()
    async with app.state.db.uow() as uow:
        runs = await uow.workflows.list_runs(workflow_id)
    assert len(runs) == 1

    # Retried / duplicate move (already in col_done) -> must NOT double-enqueue.
    async with app.state.db.uow() as uow:
        await service.move_card(card.id, "col_done", uow, actor_id="user_bilal")
        await uow.commit()
    async with app.state.db.uow() as uow:
        runs = await uow.workflows.list_runs(workflow_id)
    assert len(runs) == 1


async def test_move_to_column_without_rule_enqueues_nothing(app):
    """Moving into a column with no rule creates no run."""
    from crewspace.application.services import BoardService
    from crewspace.application.tools import build_registry

    svc = BoardService(build_registry(), app.state.settings)
    async with app.state.db.uow() as uow:
        card = await uow.boards.add_card("col_todo", "No rule", actor_id="user_bilal")
        await uow.commit()
    async with app.state.db.uow() as uow:
        await svc.move_card(card.id, "col_doing", uow, actor_id="user_bilal")
        await uow.commit()
        runs = await uow.workflows.list_enabled("chan_general", "column_move")
    assert runs == []


async def test_disabled_rule_does_not_enqueue(app):
    """A disabled rule never enqueues."""
    from crewspace.application.services import BoardService
    from crewspace.application.tools import build_registry

    workflow_id = await _seed_workflow(app)
    svc = BoardService(build_registry(), app.state.settings)
    async with app.state.db.uow() as uow:
        card = await uow.boards.add_card("col_todo", "Disabled", actor_id="user_bilal")
        from crewspace.domain.entities import ColumnWorkflowRule
        await uow.boards.set_column_workflow(
            ColumnWorkflowRule(
                id="rule_disabled", board_id="board_main", column_id="col_done",
                workflow_id=workflow_id, enabled=False, changed_by="user_bilal",
            )
        )
        await uow.commit()
    async with app.state.db.uow() as uow:
        await svc.move_card(card.id, "col_done", uow, actor_id="user_bilal")
        await uow.commit()
        runs = await uow.workflows.list_runs(workflow_id)
    assert runs == []


async def test_missing_workflow_never_enqueues_and_card_still_moves(app):
    """A rule pointing at a nonexistent workflow never silently enqueues;
    the card move itself is unaffected (no silent advance, no block)."""
    from crewspace.application.services import BoardService
    from crewspace.application.tools import build_registry

    svc = BoardService(build_registry(), app.state.settings)
    async with app.state.db.uow() as uow:
        card = await uow.boards.add_card("col_todo", "Ghost flow", actor_id="user_bilal")
        from crewspace.domain.entities import ColumnWorkflowRule
        await uow.boards.set_column_workflow(
            ColumnWorkflowRule(
                id="rule_ghost", board_id="board_main", column_id="col_done",
                workflow_id="wf_does_not_exist", enabled=True, changed_by="user_bilal",
            )
        )
        await uow.commit()

    async with app.state.db.uow() as uow:
        _, moved = await svc.move_card(card.id, "col_done", uow, actor_id="user_bilal")
        await uow.commit()
        assert moved is not None and moved.column_id == "col_done"
        rows = await uow._conn.execute(
            "SELECT COUNT(*) AS n FROM column_move_trigger"
        )
        row = await rows.fetchone()
        assert row is not None and row["n"] == 0


async def test_moving_out_of_trigger_column_does_not_enqueue(app):
    """Moving a card OUT of a rule-bound column enqueues nothing."""
    from crewspace.application.services import BoardService
    from crewspace.application.tools import build_registry

    workflow_id = await _seed_workflow(app)
    svc = BoardService(build_registry(), app.state.settings)
    async with app.state.db.uow() as uow:
        card = await uow.boards.add_card("col_todo", "Move out", actor_id="user_bilal")
        from crewspace.domain.entities import ColumnWorkflowRule
        await uow.boards.set_column_workflow(
            ColumnWorkflowRule(
                id="rule_out", board_id="board_main", column_id="col_done",
                workflow_id=workflow_id, enabled=True, changed_by="user_bilal",
            )
        )
        await uow.commit()
    async with app.state.db.uow() as uow:
        await svc.move_card(card.id, "col_done", uow, actor_id="user_bilal")
        await uow.commit()
    async with app.state.db.uow() as uow:
        assert len(await uow.workflows.list_runs(workflow_id)) == 1
    async with app.state.db.uow() as uow:
        await svc.move_card(card.id, "col_doing", uow, actor_id="user_bilal")
        await uow.commit()
    async with app.state.db.uow() as uow:
        assert len(await uow.workflows.list_runs(workflow_id)) == 1


async def test_set_column_trigger_rejects_column_from_other_board(app):
    """A rule for a column that does not belong to the submitted board must be
    rejected (no cross-board config leak)."""
    from crewspace.application.services import BoardService
    from crewspace.application.tools import build_registry
    from crewspace.domain.entities import Board, ColumnWorkflowRule

    svc = BoardService(build_registry(), app.state.settings)
    workflow_id = await _seed_workflow(app)
    async with app.state.db.uow() as uow:
        await uow.boards.create(Board(id="board_alien", workspace_id="ws_default", name="Alien"))
        alien_col = await uow.boards.create_column("board_alien", "Alien Done")
        await uow.commit()

    async with app.state.db.uow() as uow:
        try:
            await svc.set_column_trigger(
                board_id="board_main",
                column_id=alien_col.id,
                workflow_id=workflow_id,
                enabled=True,
                user={"id": "user_bilal", "role": "superadmin"},
                uow=uow,
            )
        except (PermissionError, ValueError, KeyError):
            pass
        else:
            raise AssertionError("cross-board column trigger must be rejected")


async def test_trigger_rule_bound_to_different_board_never_fires(app):
    """A stale rule whose board does not match the moved card's board must
    never enqueue a run (no cross-board trigger leak)."""
    from crewspace.application.services import BoardService
    from crewspace.application.tools import build_registry
    from crewspace.domain.entities import Board, ColumnWorkflowRule

    workflow_id = await _seed_workflow(app)
    svc = BoardService(build_registry(), app.state.settings)
    async with app.state.db.uow() as uow:
        await uow.boards.create(Board(id="board_alien", workspace_id="ws_default", name="Alien"))
        alien_col = await uow.boards.create_column("board_alien", "Alien Done")
        await uow.boards.set_column_workflow(
            ColumnWorkflowRule(
                id="rule_alien", board_id="board_main", column_id=alien_col.id,
                workflow_id=workflow_id, enabled=True, changed_by="user_bilal",
            )
        )
        card = await uow.boards.add_card("col_todo", "Cross board", actor_id="user_bilal")
        await uow.commit()

    async with app.state.db.uow() as uow:
        await svc.move_card(card.id, alien_col.id, uow, actor_id="user_bilal")
        await uow.commit()
        runs = await uow.workflows.list_runs(workflow_id)
    assert runs == []


async def test_move_card_tool_triggers_configured_workflow(app):
    """Agent/tool-originated moves use the same trigger seam as HTTP moves."""
    from crewspace.application.tools import build_registry
    from crewspace.domain.entities import ColumnWorkflowRule

    workflow_id = await _seed_workflow(app)
    async with app.state.db.uow() as uow:
        card = await uow.boards.add_card("col_todo", "Tool trigger", actor_id="user_bilal")
        await uow.boards.set_column_workflow(
            ColumnWorkflowRule(
                id="rule_tool", board_id="board_main", column_id="col_done",
                workflow_id=workflow_id, enabled=True, changed_by="user_bilal",
            )
        )
        runner = build_registry().bind(
            uow,
            principal_id="user_bilal",
            agent_id="agent_planner",
            allowed_tools={"move_card"},
        )
        await runner.run("move_card", card_id=card.id, column_id="col_done")
        await uow.commit()

    async with app.state.db.uow() as uow:
        runs = await uow.workflows.list_runs(workflow_id)
    assert len(runs) == 1
    assert runs[0].event["card_id"] == card.id


def test_board_ui_configures_and_renders_workflow_run_badge(client):
    """Settings config is usable and a moved card reflects workflow state."""
    created = client.post("/workflows", json=_rule_payload())
    assert created.status_code == 201
    workflow_id = created.json()["id"]

    settings_page = client.get("/boards/board_main/settings")
    assert settings_page.status_code == 200
    assert 'action="/boards/board_main/settings/columns/col_done/trigger"' in settings_page.text

    configured = client.post(
        "/boards/board_main/settings/columns/col_done/trigger",
        data={"workflow_id": workflow_id, "enabled": "1"},
        follow_redirects=False,
    )
    assert configured.status_code == 303

    card = client.post(
        "/boards/board_main/cards",
        data={"column_id": "col_todo", "title": "UI trigger"},
    )
    assert card.status_code == 200
    card_id = card.text.split('id="card-', 1)[1].split('"', 1)[0]
    moved = client.post(
        f"/cards/{card_id}/move",
        data={"column_id": "col_done"},
    )
    assert moved.status_code == 200

    board = client.get("/board/board_main")
    assert board.status_code == 200
    assert "Workflow: succeeded" in board.text
    assert f'href="/workflows/{workflow_id}"' in board.text


async def test_legitimate_reentry_enqueues_new_run(app):
    """Move-out then move-back is a new event, not a duplicate retry."""
    from crewspace.application.services import BoardService
    from crewspace.application.tools import build_registry
    from crewspace.domain.entities import ColumnWorkflowRule

    workflow_id = await _seed_workflow(app)
    svc = BoardService(build_registry(), app.state.settings)
    async with app.state.db.uow() as uow:
        card = await uow.boards.add_card("col_todo", "Reenter", actor_id="user_bilal")
        await uow.boards.set_column_workflow(
            ColumnWorkflowRule(
                id="rule_reentry", board_id="board_main", column_id="col_done",
                workflow_id=workflow_id, enabled=True, changed_by="user_bilal",
            )
        )
        await uow.commit()

    for target in ("col_done", "col_doing", "col_done"):
        async with app.state.db.uow() as uow:
            await svc.move_card(card.id, target, uow, actor_id="user_bilal")
            await uow.commit()

    async with app.state.db.uow() as uow:
        runs = await uow.workflows.list_runs(workflow_id)
    assert len(runs) == 2


async def test_set_column_trigger_rejects_workflow_from_other_workspace(app):
    """A board cannot be cross-wired to another workspace's workflow."""
    import datetime as dt

    from crewspace.application.services import BoardService
    from crewspace.application.tools import build_registry
    from crewspace.application.workflows import WorkflowService
    from crewspace.domain.entities import Channel, Workspace

    svc = BoardService(build_registry(), app.state.settings)
    async with app.state.db.uow() as uow:
        now = dt.datetime.now(dt.timezone.utc)
        await uow.workspaces.create_workspace(
            Workspace(
                id="ws_other", team_id="team_acme", name="Other",
                created_by="user_bilal", created_at=now,
            )
        )
        await uow.channels.create_channel(
            Channel(
                id="chan_other", workspace_id="ws_other", name="other",
                topic=None, created_by="user_bilal", created_at=now,
            )
        )
        workflow = await WorkflowService().create(
            uow,
            creator_id="user_bilal",
            data=_rule_payload(name="other_flow", channel_id="chan_other"),
        )
        await uow.commit()

    async with app.state.db.uow() as uow:
        try:
            await svc.set_column_trigger(
                board_id="board_main", column_id="col_done",
                workflow_id=workflow.id, enabled=True,
                user={"id": "user_bilal", "role": "superadmin"}, uow=uow,
            )
        except ValueError:
            pass
        else:
            raise AssertionError("cross-workspace workflow must be rejected")


def test_column_trigger_dto_is_pure_and_migration_roundtrips():
    """DTO stays infrastructure-free; legacy head upgrades/downgrades cleanly."""
    import ast
    import asyncio
    import os
    import sqlite3
    import subprocess
    import sys
    import tempfile
    from pathlib import Path

    dto_path = Path("src/crewspace/dto/board.py")
    tree = ast.parse(dto_path.read_text())
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)
    assert not any("sqlalchemy" in name or "websocket" in name for name in imported)

    from crewspace.config import Settings
    from crewspace.infrastructure.db import Database

    def alembic(command: str, target: str, url: str) -> None:
        result = subprocess.run(
            [sys.executable, "-m", "alembic", "-c", "alembic.ini", command, target],
            capture_output=True,
            text=True,
            timeout=120,
            env={**os.environ, "CREWSPACE_DATABASE_URL": url},
        )
        assert result.returncode == 0, result.stdout + result.stderr

    def tables_and_columns(path: str):
        with sqlite3.connect(path) as conn:
            tables = {
                row[0] for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
            cols = {
                row[1] for row in conn.execute("PRAGMA table_info(column_move_trigger)")
            } if "column_move_trigger" in tables else set()
        return tables, cols

    with tempfile.TemporaryDirectory() as tmp:
        db_path = os.path.join(tmp, "m75.db")
        url = f"sqlite+aiosqlite:///{db_path}"

        async def create_head() -> None:
            db = await Database.create(Settings(database_url=url))
            await db.close()

        asyncio.run(create_head())
        tables, cols = tables_and_columns(db_path)
        assert {"column_workflow_rule", "column_move_trigger"} <= tables
        assert "event_key" in cols

        alembic("downgrade", "20260830_01", url)
        tables, _ = tables_and_columns(db_path)
        assert "column_workflow_rule" not in tables
        assert "column_move_trigger" not in tables

        alembic("upgrade", "head", url)
        tables, cols = tables_and_columns(db_path)
        assert {"column_workflow_rule", "column_move_trigger"} <= tables
        assert "event_key" in cols

        # A populated downgrade must not violate the legacy workflow CHECK.
        with sqlite3.connect(db_path) as conn:
            conn.execute(
                """
                INSERT INTO workflow (
                    id, name, description, channel_id, creator_id, enabled,
                    trigger_type, trigger_config, steps, created_at, updated_at
                ) VALUES (?, ?, NULL, ?, ?, 1, 'column_move', '{}', '[]', ?, ?)
                """,
                (
                    "wf_m75_downgrade", "downgrade", "channel_general",
                    "user_bilal", "2026-08-30T00:00:00+00:00",
                    "2026-08-30T00:00:00+00:00",
                ),
            )
        alembic("downgrade", "20260830_01", url)
        with sqlite3.connect(db_path) as conn:
            row = conn.execute(
                "SELECT enabled, trigger_type FROM workflow WHERE id = ?",
                ("wf_m75_downgrade",),
            ).fetchone()
        assert row == (0, "webhook")
