"""M7.2 — board & column management + board switcher (RED phase).

These tests assert the user-visible feature BEFORE any production code exists:

Boards:
  - POST /boards creates a board in a workspace the actor can access
    (creation is scoped to the existing workspace/team hierarchy).
  - POST /boards/{id}/rename renames it; the new name reflects everywhere.
  - POST /boards/{id}/archive archives it; POST /boards/{id}/restore brings
    it back.
  - The board switcher in the sidebar lists exactly the boards the user can
    access (no cross-workspace leakage).

Columns:
  - POST /boards/{board_id}/columns adds a column at the end of the column
    order.
  - POST /boards/{board_id}/columns/{column_id}/rename renames it.
  - POST /boards/{board_id}/columns/{column_id}/reorder moves it before a
    sibling.
  - POST /boards/{board_id}/columns/{column_id}/archive hides it from the
    default board view; restore brings it back.
"""
from __future__ import annotations

import asyncio
import re
from datetime import datetime, timezone

from crewspace.application.services import BoardService
from crewspace.application.tools import build_registry
from crewspace.config import Settings
from crewspace.domain.entities import Board, Workspace, WorkspaceMembership
from crewspace.domain.ports import UnitOfWork
from crewspace.domain.entities import WorkspaceRole

_SVC = BoardService(build_registry(), Settings())


def _board_id_from_redirect(response) -> str:
    """Extract the created board id from a 303 Location header."""
    location = response.headers.get("location", "")
    m = re.search(r"/board/([A-Za-z0-9_-]+)$", location)
    assert m, f"no board id in redirect location: {location!r}"
    return m.group(1)


def _seed_workspace(app, workspace_id: str, *, member: bool = False) -> None:
    """Seed a brand-new workspace that user_bilal may or may not belong to."""

    async def _seed():
        async with app.state.db.uow() as uow:
            await uow.workspaces.create_workspace(
                Workspace(
                    id=workspace_id,
                    team_id="team_acme",
                    name=f"Workspace {workspace_id}",
                    created_by="user_bilal",
                    created_at=datetime.now(timezone.utc),
                )
            )
            if member:
                await uow.workspaces.add_member(
                    WorkspaceMembership(
                        workspace_id=workspace_id,
                        member_id="user_bilal",
                        role=WorkspaceRole.ADMIN,
                        joined_at=datetime.now(timezone.utc),
                    )
                )

    asyncio.run(_seed())


def test_board_page_has_switcher_with_default_board(client):
    """The board page must render a board switcher containing the default board."""
    r = client.get("/board/board_main")
    assert r.status_code == 200
    # Switcher surfaces the default board by id and by name.
    assert 'href="/board/board_main"' in r.text
    assert "Roadmap" in r.text


def test_board_page_switcher_lists_only_accessible_boards(client, app):
    """A brand-new board is visible in the switcher for its workspace; an
    unrelated workspace's board is NOT (no cross-workspace leakage)."""
    # Create a second board in the seeded (default) workspace, which the
    # logged-in user (Bilal) is a member of.
    r = client.post(
        "/boards", data={"workspace_id": "ws_default", "name": "Sprint 42"},
        follow_redirects=False,
    )
    assert r.status_code == 303
    # Create a board in a brand-new workspace the user does NOT belong to.
    _seed_workspace(app, "ws_alien", member=False)

    # Switch to a normal member who belongs to ws_default only. Superadmin is
    # intentionally global, so it is not a valid cross-workspace leakage actor.
    assert client.post(
        "/management/humans",
        data={"name": "Board Member", "password": "member-password", "team_id": "team_acme"},
    ).status_code == 200

    async def _grant_default_workspace():
        async with app.state.db.uow() as uow:
            member = await uow.auth.get_member_by_name("Board Member")
            assert member is not None
            await uow.workspaces.add_member(
                WorkspaceMembership(
                    workspace_id="ws_default", member_id=member["id"],
                    role=WorkspaceRole.MEMBER, joined_at=datetime.now(timezone.utc),
                )
            )

    asyncio.run(_grant_default_workspace())
    assert client.post("/auth/logout").status_code == 200
    assert client.post(
        "/auth/login", data={"username": "Board Member", "password": "member-password"},
        follow_redirects=False,
    ).status_code == 303

    r2 = client.post(
        "/boards", data={"workspace_id": "ws_alien", "name": "Alien Board"},
        follow_redirects=False,
    )
    assert r2.status_code == 404  # fail closed: not a member of that workspace

    page = client.get("/board/board_main")
    assert page.status_code == 200
    assert "Sprint 42" in page.text
    assert "Alien Board" not in page.text


def test_create_board_redirects_and_renders(client):
    r = client.post("/boards", data={"workspace_id": "ws_default", "name": "Q3 Roadmap"}, follow_redirects=False)
    assert r.status_code == 303
    r = client.get("/board/board_main")
    assert r.status_code == 200
    assert "Q3 Roadmap" in r.text


def test_new_board_page_renders(client, app):
    r = client.post("/boards", data={"workspace_id": "ws_default", "name": "Fresh Board"}, follow_redirects=False)
    assert r.status_code == 303
    board_id = _board_id_from_redirect(r)
    page = client.get(f"/board/{board_id}")
    assert page.status_code == 200
    assert "Fresh Board" in page.text


def test_rename_board_updates_switcher(client):
    client.post("/boards", data={"workspace_id": "ws_default", "name": "Old Name"})
    r = client.post(
        "/boards/board_main/rename", data={"name": "New Name"}, follow_redirects=False,
    )
    assert r.status_code == 303
    page = client.get("/board/board_main")
    assert page.status_code == 200
    assert "New Name" in page.text
    assert "New Name — Crewspace" in page.text


def test_archive_board_hides_from_switcher_and_restore_recovers(client):
    r = client.post("/boards/board_main/archive", follow_redirects=False)
    assert r.status_code == 303
    page = client.get("/board/board_main")
    # Archived board is hidden from the default board view: the page must not
    # render the board body. It remains named in the archived recovery list.
    assert page.status_code in (200, 303, 307)
    assert 'id="board-wrap"' not in page.text
    assert "archived" in page.text.lower()

    r = client.post("/boards/board_main/restore", follow_redirects=False)
    assert r.status_code == 303
    page = client.get("/board/board_main")
    assert page.status_code == 200
    assert "Roadmap" in page.text


def test_archive_board_unreachable_for_others(client, app):
    """A board outside the workspace scope must stay 404 for its board page."""
    _seed_workspace(app, "ws_alien", member=False)

    async def _seed_board():
        async with app.state.db.uow() as uow:
            await uow.boards.create(Board(id="board_alien", workspace_id="ws_alien", name="Alien Board"))

    asyncio.run(_seed_board())
    # Switch away from superadmin: superadmin intentionally sees every board.
    assert client.post("/auth/logout").status_code == 200
    assert client.post(
        "/auth/register",
        data={"username": "Board Outsider", "password": "outsider-password"},
        follow_redirects=False,
    ).status_code == 303
    r = client.get("/board/board_alien", follow_redirects=False)
    assert r.status_code == 404


async def test_board_service_creates_and_lists(app):
    async with app.state.db.uow() as uow:
        created = await _SVC.create_board("ws_default", "Service Board", uow)
        assert created.workspace_id == "ws_default"
        assert created.name == "Service Board"
        boards = await _SVC.list_accessible_boards("user_bilal", uow)
        assert any(b["id"] == created.id for b in boards)
        assert any(b["id"] == "board_main" for b in boards)


def test_columns_can_be_added(client):
    r = client.get("/board/board_main")
    assert r.status_code == 200
    assert "Backlog" not in r.text  # not seeded
    r = client.post(
        "/boards/board_main/columns", data={"name": "Backlog"}, follow_redirects=False,
    )
    assert r.status_code == 303
    page = client.get("/board/board_main")
    assert page.status_code == 200
    assert "Backlog" in page.text
    # New column is appended AFTER the seeded columns (order preserved).
    assert page.text.index("Backlog") > page.text.index("Done")


def test_column_rename(client):
    client.post("/boards/board_main/columns", data={"name": "Backlog"})
    r = client.post(
        "/boards/board_main/columns/col_todo/rename", data={"name": "Triage"},
        follow_redirects=False,
    )
    assert r.status_code == 303
    page = client.get("/board/board_main")
    assert "Triage" in page.text
    assert "To Do" not in page.text


def test_column_reorder(client):
    # Move the LAST seeded column before the FIRST: order becomes Done, To Do, In Progress.
    r = client.post(
        "/boards/board_main/columns/col_done/reorder",
        data={"before_column_id": "col_todo"}, follow_redirects=False,
    )
    assert r.status_code == 303
    page = client.get("/board/board_main")
    assert page.status_code == 200
    assert page.text.index("Done") < page.text.index("To Do")


def test_column_archive_and_restore(client):
    client.post("/boards/board_main/columns/col_doing/archive")
    page = client.get("/board/board_main")
    assert "In Progress" not in page.text

    r = client.post(
        "/boards/board_main/columns/col_doing/restore", follow_redirects=False,
    )
    assert r.status_code == 303
    page = client.get("/board/board_main")
    assert "In Progress" in page.text


async def test_board_service_rejects_empty_name(app):
    async with app.state.db.uow() as uow:
        try:
            await _SVC.create_board("ws_default", "", uow)
        except ValueError:
            pass
        else:
            raise AssertionError("empty board name must be rejected")
        try:
            await _SVC.rename_board("board_main", "", uow)
        except ValueError:
            pass
        else:
            raise AssertionError("empty board name must be rejected")


def test_new_board_uses_dedicated_app_shell_form(client):
    page = client.get("/boards/new")
    assert page.status_code == 200
    assert 'class="sidebar"' in page.text
    assert 'action="/boards"' in page.text
    assert 'name="workspace_id"' in page.text
    assert 'name="name"' in page.text


def test_board_settings_has_board_and_column_controls(client):
    page = client.get("/boards/board_main/settings")
    assert page.status_code == 200
    assert 'action="/boards/board_main/rename"' in page.text
    assert 'action="/boards/board_main/archive"' in page.text
    assert 'action="/boards/board_main/columns"' in page.text
    assert 'action="/boards/board_main/columns/col_todo/rename"' in page.text
    assert 'action="/boards/board_main/columns/col_todo/reorder"' in page.text
    assert 'action="/boards/board_main/columns/col_todo/archive"' in page.text


def test_board_column_headers_expose_management_menu(client):
    page = client.get("/board/board_main")
    assert page.status_code == 200
    assert 'aria-label="Column actions"' in page.text
    assert 'href="/boards/board_main/settings"' in page.text


def test_archived_column_is_recoverable_from_settings_ui(client):
    client.post("/boards/board_main/columns/col_doing/archive")
    page = client.get("/boards/board_main/settings")
    assert page.status_code == 200
    assert "Archived columns" in page.text
    assert "In Progress" in page.text
    assert 'action="/boards/board_main/columns/col_doing/restore"' in page.text


def test_reorder_rejects_non_active_or_foreign_target(client, app):
    missing = client.post(
        "/boards/board_main/columns/col_done/reorder",
        data={"before_column_id": "missing"}, follow_redirects=False,
    )
    assert missing.status_code == 404

    client.post("/boards/board_main/columns/col_doing/archive")
    archived = client.post(
        "/boards/board_main/columns/col_done/reorder",
        data={"before_column_id": "col_doing"}, follow_redirects=False,
    )
    assert archived.status_code == 404

    created = client.post(
        "/boards", data={"workspace_id": "ws_default", "name": "Foreign target"},
        follow_redirects=False,
    )
    other_board_id = _board_id_from_redirect(created)

    async def _first_column() -> str:
        async with app.state.db.uow() as uow:
            return next(iter((await uow.boards.list_columns(other_board_id)).values()))

    foreign_column_id = asyncio.run(_first_column())
    foreign = client.post(
        "/boards/board_main/columns/col_done/reorder",
        data={"before_column_id": foreign_column_id}, follow_redirects=False,
    )
    assert foreign.status_code == 404


def test_archived_column_rejects_new_or_moved_cards(client):
    client.post("/boards/board_main/columns/col_doing/archive")
    create = client.post(
        "/boards/board_main/cards",
        data={"column_id": "col_doing", "title": "Hidden work"},
    )
    assert create.status_code == 404

    made = client.post(
        "/boards/board_main/cards",
        data={"column_id": "col_todo", "title": "Visible work"},
    )
    assert made.status_code == 200
    match = re.search(r'id="card-([A-Za-z0-9_-]+)"', made.text)
    assert match is not None
    card_id = match.group(1)
    move = client.post(f"/cards/{card_id}/move", data={"column_id": "col_doing"})
    assert move.status_code == 404