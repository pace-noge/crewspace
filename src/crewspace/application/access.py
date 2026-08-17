"""Centralized role and team-scope authorization policies."""
from __future__ import annotations

from ..domain.ports import UnitOfWork


async def can_manage_team(user: dict, team_id: str, uow: UnitOfWork) -> bool:
    """Return whether a human may administer one team."""
    if user["role"] == "superadmin":
        return True
    membership = await uow.teams.get_membership(team_id, user["id"])
    if user["role"] == "engineering_manager":
        return membership is not None
    return membership is not None and membership.role.value == "leader"


async def manageable_teams(user: dict, uow: UnitOfWork):
    """Return exactly the teams the user may administer."""
    if user["role"] == "superadmin":
        return await uow.teams.list_teams()
    teams = await uow.teams.list_teams_for_member(user["id"])
    return [team for team in teams if await can_manage_team(user, team.id, uow)]


async def can_manage_any_team(user: dict, uow: UnitOfWork) -> bool:
    return bool(await manageable_teams(user, uow))


async def can_access_board(user: dict, board_id: str, uow: UnitOfWork) -> bool:
    """Return whether the principal may access a board's workspace."""
    if user["role"] == "superadmin":
        return await uow.boards.get_board(board_id) is not None
    board = await uow.boards.get_board(board_id)
    return bool(
        board
        and await uow.workspaces.get_membership(board.workspace_id, user["id"])
    )


async def require_board_access(user: dict, board_id: str, uow: UnitOfWork) -> None:
    """Hide boards outside the principal's workspace scope."""
    from fastapi import HTTPException

    if not await can_access_board(user, board_id, uow):
        raise HTTPException(status_code=404, detail="board not found")


async def list_accessible_boards(user: dict, uow: UnitOfWork) -> list[dict[str, str]]:
    """Boards the principal may act on, as {id, name, team} dicts.

    Superadmin sees every board; everyone else sees only boards whose
    workspace they belong to. Used by the agent so it can resolve "the board"
    without asking for an id, and so higher-tier roles (superadmin /
    engineering_manager) can be shown the menu of boards they manage.
    """
    if user["role"] == "superadmin":
        boards = await uow.boards.list_all()
    else:
        boards = await uow.boards.list_for_member(user["id"])
    return [{"id": b.id, "name": b.name, "team": b.team_name or ""} for b in boards]
