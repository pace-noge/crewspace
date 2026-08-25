"""API: operational inbox app-shell and local-state actions."""
from __future__ import annotations

from fastapi import APIRouter, Form, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from ...application.inbox import InboxFilters
from ...application.inbox_store import inbox_store
from ..deps import CurrentUserOptionalDep, UowDep, require_member_redirect
from ..rendering import navigation_context, templates

router = APIRouter(prefix="/inbox", tags=["inbox"])


def _record(source_type: str, source, *, summary: str) -> dict:
    created_at = getattr(source, "created_at", "")
    isoformat = getattr(created_at, "isoformat", None)
    return {
        "source_type": source_type,
        "source_id": source.id,
        "status": source.status,
        "team_id": source.team_id,
        "summary": summary,
        "created_at": isoformat() if callable(isoformat) else str(created_at),
    }


async def _authorized_team(uow, current_user: dict, requested_team_id: str | None):
    teams = await uow.teams.list_teams_for_member(current_user["id"])
    by_id = {team.id: team for team in teams}
    if requested_team_id is not None:
        team = by_id.get(requested_team_id)
        if team is None:
            raise HTTPException(status_code=404, detail="Inbox not found")
        return team
    if not teams:
        raise HTTPException(status_code=404, detail="Inbox not found")
    return teams[0]


async def _source_records(uow, team_id: str) -> list[dict]:
    runs = await uow.coding_runs.list_for_team(team_id)
    change_sets = await uow.change_sets.list_for_teams([team_id])
    records = [
        _record("coding_run", run, summary=run.failure_reason or run.instruction)
        for run in runs
    ]
    records.extend(
        _record("change_set", change_set, summary=f"Review change set {change_set.id}")
        for change_set in change_sets
    )
    return records


@router.get("", response_class=HTMLResponse)
async def inbox_page(
    request: Request,
    uow: UowDep,
    current_user: CurrentUserOptionalDep,
    team_id: str | None = Query(default=None),
    kind: list[str] = Query(default=[]),
    unacknowledged: bool = Query(default=False),
    unresolved: bool = Query(default=True),
    min_priority: int | None = Query(default=None),
) -> Response:
    redirect = require_member_redirect(current_user)
    if redirect is not None:
        return redirect
    assert current_user is not None
    team = await _authorized_team(uow, current_user, team_id)
    records = await _source_records(uow, team.id)
    inbox_store.reconcile(team.id, records)
    filters = InboxFilters(
        kinds=tuple(kind) if kind else None,
        only_unacknowledged=unacknowledged,
        only_unresolved=unresolved,
        min_priority=min_priority,
    )
    view = inbox_store.view(team.id, filters)
    return templates.TemplateResponse(
        request=request,
        name="inbox.html",
        context={
            "current_user": current_user,
            "team": team,
            "view": view,
            "all_kinds": sorted({item.kind for item in inbox_store.view(team.id).items}),
            **await navigation_context(uow, current_user),
        },
    )


def _redirect(team_id: str) -> RedirectResponse:
    return RedirectResponse(f"/inbox?team_id={team_id}", status_code=303)


@router.post("/{item_id}/acknowledge")
async def acknowledge_inbox_item(
    item_id: str,
    uow: UowDep,
    current_user: CurrentUserOptionalDep,
    team_id: str = Form(...),
) -> Response:
    redirect = require_member_redirect(current_user)
    if redirect is not None:
        return redirect
    assert current_user is not None
    team = await _authorized_team(uow, current_user, team_id)
    if not inbox_store.acknowledge(team.id, item_id):
        raise HTTPException(status_code=404, detail="Inbox item not found")
    return _redirect(team.id)


@router.post("/{item_id}/assign")
async def assign_inbox_item(
    item_id: str,
    uow: UowDep,
    current_user: CurrentUserOptionalDep,
    team_id: str = Form(...),
    owner_id: str = Form(...),
) -> Response:
    redirect = require_member_redirect(current_user)
    if redirect is not None:
        return redirect
    assert current_user is not None
    team = await _authorized_team(uow, current_user, team_id)
    if not inbox_store.assign(team.id, item_id, owner_id.strip()):
        raise HTTPException(status_code=404, detail="Inbox item not found")
    return _redirect(team.id)


@router.post("/{item_id}/resolve")
async def resolve_inbox_item(
    item_id: str,
    uow: UowDep,
    current_user: CurrentUserOptionalDep,
    team_id: str = Form(...),
) -> Response:
    redirect = require_member_redirect(current_user)
    if redirect is not None:
        return redirect
    assert current_user is not None
    team = await _authorized_team(uow, current_user, team_id)
    if not inbox_store.resolve(team.id, item_id):
        raise HTTPException(status_code=404, detail="Inbox item not found")
    return _redirect(team.id)
