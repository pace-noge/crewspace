"""Team-scoped management UI for remote coding change sets."""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from pydantic import ValidationError

from ...application.access import can_manage_team, manageable_teams
from ...application.change_sets import ChangeSetService, execute_workspace_decision
from ...config import get_settings
from ...dto.change_sets import ChangeSetDTO
from ..connection import agent_manager
from ..deps import CurrentUserDep, UowDep
from ..rendering import navigation_context, templates

router = APIRouter(prefix="/management/change-sets", tags=["change-sets"])

ACTION_PAGES = {
    "review": {
        "title": "Review change set",
        "submit_label": "Mark reviewed",
        "description": "Confirm that this captured change set has been reviewed before choosing a delivery or retention decision.",
        "expected_status": "captured",
    },
    "request-pr": {
        "title": "Request PR",
        "submit_label": "Request pull request",
        "description": "Record approval to ask the remote coding agent to create a pull request.",
        "expected_status": "reviewed",
    },
    "retain": {
        "title": "Retain workspace",
        "submit_label": "Retain workspace",
        "description": "Protect this remote workspace from automatic cleanup.",
        "expected_status": "reviewed",
    },
    "request-discard": {
        "title": "Request discard",
        "submit_label": "Request discard",
        "description": "Record approval to ask the remote coding agent for safe cleanup.",
        "expected_status": "reviewed",
    },
}


async def _managed_change_set(change_set_id: str, current_user: dict, uow: UowDep):
    stored = await uow.change_sets.get(change_set_id)
    if stored is None:
        raise HTTPException(status_code=404, detail="Change set not found")
    if not await can_manage_team(current_user, stored.team_id, uow):
        raise HTTPException(status_code=403, detail="You cannot manage this change set")
    return stored


@router.get("", response_class=HTMLResponse)
async def change_set_index_page(
    request: Request, current_user: CurrentUserDep, uow: UowDep
):
    teams = await manageable_teams(current_user, uow)
    records = await uow.change_sets.list_for_teams([team.id for team in teams])
    return templates.TemplateResponse(
        request=request,
        name="change_sets.html",
        context={
            "request": request,
            "current_user": current_user,
            "records": records,
            **await navigation_context(uow, current_user),
        },
    )


@router.get("/{change_set_id}/request-pr", response_class=HTMLResponse)
@router.get("/{change_set_id}/retain", response_class=HTMLResponse)
@router.get("/{change_set_id}/request-discard", response_class=HTMLResponse)
@router.get("/{change_set_id}/review", response_class=HTMLResponse)
async def change_set_action_page(
    request: Request,
    change_set_id: str,
    current_user: CurrentUserDep,
    uow: UowDep,
):
    endpoint = request.url.path.rsplit("/", 1)[-1]
    page = ACTION_PAGES[endpoint]
    stored = await _managed_change_set(change_set_id, current_user, uow)
    if stored.status != page["expected_status"]:
        raise HTTPException(
            status_code=409, detail="Change set is not ready for this action"
        )
    try:
        change_set = ChangeSetDTO.model_validate(stored.payload)
    except ValidationError as exc:
        raise HTTPException(status_code=422, detail="Stored change set is invalid") from exc
    return templates.TemplateResponse(
        request=request,
        name="change_set_action.html",
        context={
            "request": request,
            "current_user": current_user,
            "stored": stored,
            "change_set": change_set,
            "endpoint": endpoint,
            **{key: value for key, value in page.items() if key != "expected_status"},
            **await navigation_context(uow, current_user),
        },
    )


@router.get("/{change_set_id}", response_class=HTMLResponse)
async def change_set_detail_page(
    request: Request,
    change_set_id: str,
    current_user: CurrentUserDep,
    uow: UowDep,
):
    stored = await _managed_change_set(change_set_id, current_user, uow)
    try:
        change_set = ChangeSetDTO.model_validate(stored.payload)
    except ValidationError as exc:
        raise HTTPException(status_code=422, detail="Stored change set is invalid") from exc
    audit = await uow.change_sets.list_audit(stored.id)
    return templates.TemplateResponse(
        request=request,
        name="change_set_detail.html",
        context={
            "request": request,
            "current_user": current_user,
            "stored": stored,
            "change_set": change_set,
            "audit": audit,
            **await navigation_context(uow, current_user),
        },
    )


@router.post("/{change_set_id}/review")
async def review_change_set(
    change_set_id: str, current_user: CurrentUserDep, uow: UowDep
) -> RedirectResponse:
    await _managed_change_set(change_set_id, current_user, uow)
    try:
        await ChangeSetService().review(
            change_set_id=change_set_id, actor_id=current_user["id"], uow=uow
        )
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    await uow.commit()
    return RedirectResponse(
        f"/management/change-sets/{change_set_id}", status_code=303
    )


async def _record_decision(
    change_set_id: str,
    decision: str,
    current_user: dict,
    uow: UowDep,
) -> RedirectResponse:
    await _managed_change_set(change_set_id, current_user, uow)
    try:
        await ChangeSetService().decide(
            change_set_id=change_set_id,
            decision=decision,
            actor_id=current_user["id"],
            uow=uow,
        )
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    await uow.commit()
    return RedirectResponse(
        f"/management/change-sets/{change_set_id}", status_code=303
    )


@router.post("/{change_set_id}/request-pr")
async def request_change_set_pr(
    change_set_id: str, current_user: CurrentUserDep, uow: UowDep
) -> RedirectResponse:
    return await _record_decision(change_set_id, "request_pr", current_user, uow)


@router.post("/{change_set_id}/retain")
async def retain_change_set_workspace(
    request: Request, change_set_id: str, current_user: CurrentUserDep
) -> RedirectResponse:
    try:
        await execute_workspace_decision(
            db=request.app.state.db,
            manager=agent_manager,
            change_set_id=change_set_id,
            decision="retain",
            current_user=current_user,
            timeout=get_settings().agent_reply_timeout,
        )
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except (KeyError, ConnectionError, RuntimeError, TimeoutError) as exc:
        raise HTTPException(status_code=502, detail="Remote workspace action failed") from exc
    return RedirectResponse(
        f"/management/change-sets/{change_set_id}", status_code=303
    )


@router.post("/{change_set_id}/request-discard")
async def request_change_set_discard(
    request: Request, change_set_id: str, current_user: CurrentUserDep
) -> RedirectResponse:
    try:
        await execute_workspace_decision(
            db=request.app.state.db,
            manager=agent_manager,
            change_set_id=change_set_id,
            decision="request_discard",
            current_user=current_user,
            timeout=get_settings().agent_reply_timeout,
        )
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except (KeyError, ConnectionError, RuntimeError, TimeoutError) as exc:
        raise HTTPException(status_code=502, detail="Remote workspace action failed") from exc
    return RedirectResponse(
        f"/management/change-sets/{change_set_id}", status_code=303
    )
