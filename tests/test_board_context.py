"""Board context for the builtin agent.

Verifies that a principal with a single board never has to supply a board id
(the agent auto-resolves it), while a principal with several boards is shown
the menu (option A), and that ``list_boards`` exposes those boards.
"""
from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path

import pytest

from crewspace.application.access import list_accessible_boards
from crewspace.application.tools import build_registry
from crewspace.config import Settings
from crewspace.domain.identifiers import DEFAULT_BOARD_ID
from crewspace.infrastructure.db import Database


@pytest.fixture
def settings():
    return Settings(db_path=str(Path(tempfile.mkdtemp()) / "ctx.db"))


async def _make_db(settings):
    return await Database.create(settings)


async def _seed_second_board(db, uow):
    # A second workspace (same team) + board, with the seeded user as member.
    await uow._conn.execute(
        "INSERT INTO workspace (id, team_id, name, created_by, created_at) "
        "VALUES (?, ?, ?, ?, ?)",
        ("ws_second", "team_acme", "Second WS", "user_bilal", "2026-01-01T00:00:00Z"),
    )
    await uow._conn.execute(
        "INSERT INTO board (id, workspace_id, name) VALUES (?, ?, ?)",
        ("board_second", "ws_second", "Second Board"),
    )
    await uow._conn.execute(
        "INSERT INTO workspace_member (workspace_id, member_id, role, joined_at) "
        "VALUES (?, ?, ?, ?)",
        ("ws_second", "user_bilal", "admin", "2026-01-01T00:00:00Z"),
    )
    await uow.commit()


@pytest.mark.asyncio
async def test_single_board_auto_resolves_without_id(settings):
    db = await _make_db(settings)
    try:
        async with db.uow() as uow:
            reg = build_registry()
            runner = reg.bind(uow, principal_id="user_bilal")
            # No board_id supplied; with exactly one board it must resolve
            # automatically instead of asking for an id.
            result = await runner.run("find_card", title="Does not exist", board_id=None)
        assert result is None
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_list_boards_returns_callers_boards(settings):
    db = await _make_db(settings)
    try:
        async with db.uow() as uow:
            reg = build_registry()
            runner = reg.bind(uow, principal_id="user_bilal")
            boards = await runner.run("list_boards")
        ids = {b["id"] for b in boards}
        assert DEFAULT_BOARD_ID in ids
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_multi_board_lists_options_instead_of_guessing(settings):
    db = await _make_db(settings)
    try:
        async with db.uow() as uow:
            await _seed_second_board(db, uow)
            reg = build_registry()
            runner = reg.bind(uow, principal_id="user_bilal")
            # Two boards now accessible with no board_id -> the agent is shown
            # the menu (a PermissionError whose message lists the boards).
            try:
                await runner.run("find_card", title="x", board_id=None)
            except PermissionError as exc:
                msg = str(exc)
            else:
                msg = "NO_ERROR"
        assert "NO_ERROR" not in msg
        assert "board_main" in msg and "board_second" in msg
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_list_accessible_boards_count(settings):
    db = await _make_db(settings)
    try:
        async with db.uow() as uow:
            await _seed_second_board(db, uow)
            principal = await uow.auth.get_member("user_bilal")
            boards = await list_accessible_boards(principal, uow)
        ids = [b["id"] for b in boards]
        assert set(ids) == {DEFAULT_BOARD_ID, "board_second"}
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_list_boards_without_principal_exposes_boards(settings):
    # The MCP server binds the runner WITHOUT a principal (agent/system context).
    # list_boards must still surface the boards (names only) rather than return [].
    db = await _make_db(settings)
    try:
        async with db.uow() as uow:
            reg = build_registry()
            runner = reg.bind(uow)  # no principal_id
            boards = await runner.run("list_boards")
        assert any(b["id"] == DEFAULT_BOARD_ID for b in boards)
    finally:
        await db.close()
