"""Guarded UI and actions for scheduled channel instructions."""
from __future__ import annotations

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from ...application.access import manageable_teams
from ...application.scheduling import (
    ScheduledJobService,
    can_access_job,
    can_create_channel_job,
    can_manage_channel_job,
    schedule_label,
)
from ..connection import manager
from ..deps import CurrentUserDep, CurrentUserOptionalDep, UowDep, require_member_redirect
from ..rendering import navigation_context, templates

router = APIRouter(prefix="/cronjobs", tags=["cronjobs"])


async def _manageable_channels(user: dict, uow) -> list:
    if user["role"] == "team_member":
        channels = []
        for channel in await uow.channels.list_channels_for_member(user["id"]):
            workspace = await uow.workspaces.get_workspace(channel.workspace_id)
            team = await uow.teams.get_team(workspace.team_id) if workspace else None
            if workspace and team:
                channels.append({"channel": channel, "workspace": workspace, "team": team})
        return channels
    channels = []
    for team in await manageable_teams(user, uow):
        for workspace in await uow.workspaces.list_workspaces_for_team(team.id):
            for channel in await uow.channels.list_channels_for_workspace(workspace.id):
                channels.append({"channel": channel, "workspace": workspace, "team": team})
    return channels


async def _context(request: Request, user: dict, uow, focused_id: str | None = None) -> dict:
    channels = await _manageable_channels(user, uow)
    if not channels and user["role"] != "superadmin":
        raise HTTPException(status_code=403, detail="You cannot manage scheduled instructions")
    channel_ids = [item["channel"].id for item in channels]
    jobs = await uow.scheduled_jobs.list_for_channels(channel_ids)
    if user["role"] == "team_member":
        jobs = [job for job in jobs if job.creator_id == user["id"]]
    run_history = await uow.scheduled_jobs.list_runs(focused_id) if focused_id else []
    return {
        "request": request,
        "current_user": user,
        "agents": await uow.auth.list_members(kind="agent"),
        "cronjob_channels": channels,
        "jobs": [{"job": job, "schedule_label": schedule_label(job)} for job in jobs],
        "focused_id": focused_id,
        "run_history": run_history,
        **await navigation_context(uow, user),
    }


@router.get("", response_class=HTMLResponse)
async def list_jobs(request: Request, current_user: CurrentUserOptionalDep, uow: UowDep) -> Response:
    redirect = require_member_redirect(current_user)
    if redirect is not None:
        return redirect
    assert current_user is not None
    return templates.TemplateResponse(
        request=request, name="cronjobs.html",
        context=await _context(request, current_user, uow),
    )


@router.get("/new", response_class=HTMLResponse)
async def new_job(request: Request, current_user: CurrentUserOptionalDep, uow: UowDep) -> Response:
    redirect = require_member_redirect(current_user)
    if redirect is not None:
        return redirect
    assert current_user is not None
    context = await _context(request, current_user, uow)
    context.update({"job": None, "form_title": "New scheduled instruction", "form_action": "/cronjobs"})
    return templates.TemplateResponse(request=request, name="cronjob_form.html", context=context)


@router.post("")
async def create_job(
    request: Request,
    current_user: CurrentUserDep,
    uow: UowDep,
    channel_id: str = Form(...),
    name: str = Form(...),
    description: str = Form(""),
    instruction: str = Form(...),
    schedule_kind: str = Form(...),
    interval_value: str = Form(""),
    interval_unit: str = Form("hours"),
    daily_time: str = Form(""),
    run_at: str = Form(""),
):
    if not await can_create_channel_job(current_user, channel_id, uow):
        raise HTTPException(status_code=403, detail="You cannot schedule instructions for this channel")
    try:
        job = await ScheduledJobService(request.app.state.settings).create(
            uow,
            name=name,
            description=description or None,
            channel_id=channel_id,
            instruction=instruction,
            schedule_kind=schedule_kind,
            creator_id=current_user["id"],
            interval_value=int(interval_value) if interval_value else None,
            interval_unit=interval_unit or None,
            daily_time=daily_time or None,
            run_at=run_at or None,
        )
        await uow.commit()
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return RedirectResponse(f"/cronjobs/{job.id}", status_code=303)


async def _authorized_job(job_id: str, user: dict, uow, action: str):
    job = await uow.scheduled_jobs.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Scheduled instruction not found")
    if not await can_access_job(user, job, uow):
        raise HTTPException(status_code=403, detail=f"You cannot {action} this scheduled instruction")
    return job


@router.get("/{job_id}/edit", response_class=HTMLResponse)
async def edit_job(job_id: str, request: Request, current_user: CurrentUserOptionalDep, uow: UowDep) -> Response:
    redirect = require_member_redirect(current_user)
    if redirect is not None:
        return redirect
    assert current_user is not None
    job = await _authorized_job(job_id, current_user, uow, "edit")
    context = await _context(request, current_user, uow)
    context.update({"job": job, "form_title": "Edit scheduled instruction", "form_action": f"/cronjobs/{job.id}"})
    return templates.TemplateResponse(request=request, name="cronjob_form.html", context=context)


@router.post("/{job_id}")
async def update_job(
    job_id: str, request: Request, current_user: CurrentUserDep, uow: UowDep,
    channel_id: str = Form(...), name: str = Form(...), description: str = Form(""),
    instruction: str = Form(...), schedule_kind: str = Form(...),
    interval_value: str = Form(""), interval_unit: str = Form("hours"),
    daily_time: str = Form(""), run_at: str = Form(""),
):
    job = await _authorized_job(job_id, current_user, uow, "edit")
    if not await can_create_channel_job(current_user, channel_id, uow):
        raise HTTPException(status_code=403, detail="You cannot schedule instructions for this channel")
    try:
        await ScheduledJobService(request.app.state.settings).update(
            job, uow, name=name, description=description or None, channel_id=channel_id,
            instruction=instruction, schedule_kind=schedule_kind,
            interval_value=int(interval_value) if interval_value else None,
            interval_unit=interval_unit or None, daily_time=daily_time or None, run_at=run_at or None,
        )
        await uow.commit()
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return RedirectResponse("/cronjobs", status_code=303)


@router.post("/{job_id}/pause")
async def pause_job(job_id: str, request: Request, current_user: CurrentUserDep, uow: UowDep):
    job = await _authorized_job(job_id, current_user, uow, "pause")
    await ScheduledJobService(request.app.state.settings).pause(job, uow)
    await uow.commit()
    return RedirectResponse("/cronjobs", status_code=303)


@router.post("/{job_id}/resume")
async def resume_job(job_id: str, request: Request, current_user: CurrentUserDep, uow: UowDep):
    job = await _authorized_job(job_id, current_user, uow, "resume")
    await ScheduledJobService(request.app.state.settings).resume(job, uow)
    await uow.commit()
    return RedirectResponse("/cronjobs", status_code=303)


@router.post("/{job_id}/delete")
async def delete_job(job_id: str, request: Request, current_user: CurrentUserDep, uow: UowDep):
    job = await _authorized_job(job_id, current_user, uow, "delete")
    await ScheduledJobService(request.app.state.settings).delete(job, uow)
    await uow.commit()
    return RedirectResponse("/cronjobs", status_code=303)


@router.get("/{job_id}/history", response_class=HTMLResponse)
async def job_history(
    job_id: str, request: Request, current_user: CurrentUserOptionalDep, uow: UowDep
) -> Response:
    redirect = require_member_redirect(current_user)
    if redirect is not None:
        return redirect
    assert current_user is not None
    job = await uow.scheduled_jobs.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Scheduled instruction not found")
    if not await can_access_job(current_user, job, uow):
        raise HTTPException(status_code=403, detail="You cannot view this scheduled instruction")
    return templates.TemplateResponse(
        request=request, name="cronjobs.html",
        context=await _context(request, current_user, uow, job_id),
    )


@router.get("/{job_id}")
async def legacy_job_detail(job_id: str, current_user: CurrentUserOptionalDep, uow: UowDep) -> Response:
    redirect = require_member_redirect(current_user)
    if redirect is not None:
        return redirect
    assert current_user is not None
    await _authorized_job(job_id, current_user, uow, "view")
    return RedirectResponse(f"/cronjobs/{job_id}/history", status_code=307)


@router.get("/{job_id}/runs/{run_id}", response_class=HTMLResponse)
async def run_detail(
    job_id: str, run_id: str, request: Request,
    current_user: CurrentUserOptionalDep, uow: UowDep,
) -> Response:
    redirect = require_member_redirect(current_user)
    if redirect is not None:
        return redirect
    assert current_user is not None
    job = await uow.scheduled_jobs.get(job_id)
    run = await uow.scheduled_jobs.get_run(run_id)
    if job is None or run is None or run.job_id != job.id:
        raise HTTPException(status_code=404, detail="Run not found")
    if not await can_access_job(current_user, job, uow):
        raise HTTPException(status_code=403, detail="You cannot view this run")
    return templates.TemplateResponse(
        request=request, name="cronjob_run.html",
        context={
            "request": request, "current_user": current_user, "job": job, "run": run,
            "agents": await uow.auth.list_members(kind="agent"),
            **await navigation_context(uow, current_user),
        },
    )


@router.post("/{job_id}/run")
async def run_job(
    job_id: str, request: Request, current_user: CurrentUserDep, uow: UowDep
):
    job = await uow.scheduled_jobs.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Scheduled instruction not found")
    if not await can_access_job(current_user, job, uow):
        raise HTTPException(status_code=403, detail="You cannot run this scheduled instruction")
    try:
        messages = await ScheduledJobService(request.app.state.settings).run(
            job, uow, trigger="manual", initiated_by=current_user["id"]
        )
        await uow.commit()
    except Exception as exc:
        await uow.commit()
        raise HTTPException(status_code=500, detail=f"Scheduled instruction failed: {exc}") from exc
    for message in messages:
        await manager.broadcast(job.channel_id, message.model_dump(mode="json"))
    return RedirectResponse("/cronjobs", status_code=303)
