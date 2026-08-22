"""Workflow builder, API, run history, webhooks, and approvals."""
from __future__ import annotations

from dataclasses import asdict
import csv
import hashlib
import io
import secrets

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from pydantic import BaseModel, Field

from ...application.workflows import WorkflowService
from ...dto.mappers import to_message
from ...infrastructure.mcp_client import ExternalMcpToolExecutor
from ...infrastructure.workflow_webhooks import build_workflow_webhook_executor
from ..connection import manager
from ..deps import CurrentUserDep, CurrentUserOptionalDep, UowDep, require_member_redirect
from ..rendering import navigation_context, templates

router = APIRouter(prefix="/workflows", tags=["workflows"])
hooks_router = APIRouter(tags=["workflow-hooks"])


async def _broadcast_workflow_message(message) -> None:
    await manager.broadcast(
        message.channel_id, to_message(message).model_dump(mode="json")
    )


async def _broadcast_workflow_progress(event: dict) -> None:
    channel_id = event.get("channel_id")
    if channel_id:
        await manager.broadcast(channel_id, event)


def _workflow_service() -> WorkflowService:
    return WorkflowService(
        on_message=_broadcast_workflow_message,
        on_progress=_broadcast_workflow_progress,
        webhook_executor=build_workflow_webhook_executor(),
        mcp_executor=ExternalMcpToolExecutor(),
    )


class WorkflowStepInput(BaseModel):
    id: str
    name: str | None = None
    action: str
    timeout_seconds: int | None = None
    condition: str | None = None
    config: dict = Field(default_factory=dict)


class WorkflowInput(BaseModel):
    name: str
    description: str | None = None
    channel_id: str
    enabled: bool = True
    trigger_type: str
    trigger_config: dict = Field(default_factory=dict)
    filter_expression: str | None = None
    steps: list[WorkflowStepInput]


@router.get("/api")
async def workflows_api(current_user: CurrentUserDep, uow: UowDep) -> list[dict]:
    channels = await uow.channels.list_channels_for_member(current_user["id"])
    return [_workflow_json(item) for item in await uow.workflows.list_for_channels(
        [channel.id for channel in channels]
    )]


@hooks_router.post("/hooks/{hook_id}", status_code=202)
async def workflow_hook(hook_id: str, request: Request, uow: UowDep) -> dict:
    workflow = await uow.workflows.get_by_hook_id(hook_id)
    if workflow is None or workflow.trigger_type != "webhook" or not workflow.enabled:
        raise HTTPException(status_code=404, detail="Webhook not found")
    supplied = request.headers.get("X-Webhook-Secret", "")
    expected = workflow.trigger_config.get("secret_hash", "")
    if not supplied or not secrets.compare_digest(hashlib.sha256(supplied.encode()).hexdigest(), expected):
        raise HTTPException(status_code=401, detail="Invalid webhook secret")
    payload = await request.json()
    runs = await _workflow_service().dispatch(
        uow, channel_id=workflow.channel_id, trigger_type="webhook", event=payload
    )
    return {"run_ids": [run.id for run in runs]}


@router.post("/approvals/{token}/approve")
async def approve_workflow(token: str, current_user: CurrentUserDep, uow: UowDep) -> dict:
    pending = await uow.workflows.get_run_by_approval(token)
    workflow = await uow.workflows.get(pending.workflow_id) if pending else None
    if pending is None or workflow is None:
        raise HTTPException(status_code=404, detail="Approval not found")
    if not await uow.channels.can_member_access(workflow.channel_id, current_user["id"]):
        raise HTTPException(status_code=403, detail="You cannot approve this workflow")
    try:
        run = await _workflow_service().approve(token, uow, True)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return _run_json(run)


@router.post("/approvals/{token}/reject")
async def reject_workflow(token: str, current_user: CurrentUserDep, uow: UowDep) -> dict:
    pending = await uow.workflows.get_run_by_approval(token)
    workflow = await uow.workflows.get(pending.workflow_id) if pending else None
    if pending is None or workflow is None:
        raise HTTPException(status_code=404, detail="Approval not found")
    if not await uow.channels.can_member_access(workflow.channel_id, current_user["id"]):
        raise HTTPException(status_code=403, detail="You cannot reject this workflow")
    try:
        run = await _workflow_service().approve(token, uow, False)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return _run_json(run)


def _workflow_json(workflow) -> dict:
    result = asdict(workflow)
    result["trigger_config"] = {
        key: value for key, value in result["trigger_config"].items()
        if key != "secret_hash"
    }
    for key in ("created_at", "updated_at"):
        if result[key] is not None:
            result[key] = result[key].isoformat()
    return result


def _run_json(run) -> dict:
    result = asdict(run)
    result["status"] = run.status.value
    result["started_at"] = run.started_at.isoformat()
    result["finished_at"] = run.finished_at.isoformat() if run.finished_at else None
    return result


async def _page_context(request: Request, user: dict, uow) -> dict:
    channels = await uow.channels.list_channels_for_member(user["id"])
    workflows = await uow.workflows.list_for_channels([channel.id for channel in channels])
    pending_approvals = await uow.workflows.list_pending_approvals(
        [workflow.id for workflow in workflows]
    )
    return {
        "request": request, "current_user": user, "channels": channels,
        "workflows": workflows, "pending_approvals": pending_approvals,
        **await navigation_context(uow, user),
    }


async def _manageable_workflow(workflow_id: str, user: dict, uow):
    workflow = await uow.workflows.get(workflow_id)
    if workflow is None:
        raise HTTPException(status_code=404, detail="Workflow not found")
    if not await uow.channels.can_member_access(workflow.channel_id, user["id"]):
        raise HTTPException(status_code=404, detail="Workflow not found")
    if workflow.creator_id != user["id"] and user["role"] != "superadmin":
        raise HTTPException(status_code=403, detail="You cannot manage this workflow")
    return workflow


async def _visible_workflow(workflow_id: str, user: dict, uow):
    workflow = await uow.workflows.get(workflow_id)
    if workflow is None or not await uow.channels.can_member_access(
        workflow.channel_id, user["id"]
    ):
        raise HTTPException(status_code=404, detail="Workflow not found")
    return workflow


@router.get("", response_class=HTMLResponse)
async def list_workflows(request: Request, current_user: CurrentUserOptionalDep, uow: UowDep) -> Response:
    redirect = require_member_redirect(current_user)
    if redirect is not None:
        return redirect
    assert current_user is not None
    return templates.TemplateResponse(
        request=request, name="workflows.html", context=await _page_context(request, current_user, uow)
    )


@router.get("/new", response_class=HTMLResponse)
async def new_workflow(request: Request, current_user: CurrentUserOptionalDep, uow: UowDep) -> Response:
    redirect = require_member_redirect(current_user)
    if redirect is not None:
        return redirect
    assert current_user is not None
    return templates.TemplateResponse(
        request=request, name="workflow_form.html", context={
            **await _page_context(request, current_user, uow), "workflow": None,
        }
    )


@router.get("/{workflow_id}", response_class=HTMLResponse)
async def workflow_detail(
    workflow_id: str, request: Request,
    current_user: CurrentUserOptionalDep, uow: UowDep,
) -> Response:
    redirect = require_member_redirect(current_user)
    if redirect is not None:
        return redirect
    assert current_user is not None
    workflow = await _visible_workflow(workflow_id, current_user, uow)
    step_names = {
        step["id"]: step.get("name") or step["id"] for step in workflow.steps
    }
    run_views = []
    for run in await uow.workflows.list_runs(workflow.id):
        duration_ms = None
        if run.finished_at is not None:
            duration_ms = int((run.finished_at - run.started_at).total_seconds() * 1000)
        run_views.append({
            "run": run,
            "duration_ms": duration_ms,
            "initiated_by": run.event.get("initiated_by"),
            "can_retry": (
                run.status.value == "failed"
                and run.current_step < len(workflow.steps)
            ),
            "results": [
                {
                    "name": step_names.get(result["step_id"], result["step_id"]),
                    "status": result["status"],
                }
                for result in run.step_results
            ],
        })
    return templates.TemplateResponse(
        request=request,
        name="workflow_detail.html",
        context={
            **await _page_context(request, current_user, uow),
            "workflow": workflow,
            "run_views": run_views,
            "can_manage": (
                workflow.creator_id == current_user["id"]
                or current_user["role"] == "superadmin"
            ),
        },
    )


@router.get("/{workflow_id}/edit", response_class=HTMLResponse)
async def edit_workflow_page(
    workflow_id: str, request: Request,
    current_user: CurrentUserOptionalDep, uow: UowDep,
) -> Response:
    redirect = require_member_redirect(current_user)
    if redirect is not None:
        return redirect
    assert current_user is not None
    workflow = await _manageable_workflow(workflow_id, current_user, uow)
    return templates.TemplateResponse(
        request=request, name="workflow_form.html", context={
            **await _page_context(request, current_user, uow),
            "workflow": _workflow_json(workflow),
        },
    )


@router.post("", status_code=201)
async def create_workflow(
    payload: WorkflowInput, request: Request,
    current_user: CurrentUserDep, uow: UowDep,
) -> dict:
    if not await uow.channels.can_member_access(payload.channel_id, current_user["id"]):
        raise HTTPException(status_code=403, detail="You cannot create workflows for this channel")
    try:
        data = payload.model_dump()
        webhook_secret = None
        hook_id = None
        if payload.trigger_type == "webhook":
            hook_id = secrets.token_urlsafe(18)
            webhook_secret = secrets.token_urlsafe(32)
            data["trigger_config"] = {
                "hook_id": hook_id,
                "secret_hash": hashlib.sha256(webhook_secret.encode()).hexdigest(),
            }
        workflow = await WorkflowService().create(
            uow, creator_id=current_user["id"], data=data
        )
        await uow.commit()
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    result = _workflow_json(workflow)
    if webhook_secret and hook_id:
        result["webhook"] = {
            "url": str(request.base_url).rstrip("/") + f"/hooks/{hook_id}",
            "secret": webhook_secret,
        }
    return result


@router.put("/{workflow_id}")
async def update_workflow(
    workflow_id: str, payload: WorkflowInput,
    current_user: CurrentUserDep, uow: UowDep,
) -> dict:
    workflow = await _manageable_workflow(workflow_id, current_user, uow)
    if not await uow.channels.can_member_access(payload.channel_id, current_user["id"]):
        raise HTTPException(status_code=403, detail="You cannot move this workflow to that channel")
    data = payload.model_dump()
    if payload.trigger_type == "webhook":
        if workflow.trigger_type == "webhook":
            data["trigger_config"] = workflow.trigger_config
        else:
            secret = secrets.token_urlsafe(32)
            data["trigger_config"] = {
                "hook_id": secrets.token_urlsafe(18),
                "secret_hash": hashlib.sha256(secret.encode()).hexdigest(),
            }
    try:
        updated = await WorkflowService().update(uow, workflow=workflow, data=data)
        await uow.commit()
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    return _workflow_json(updated)


@router.post("/{workflow_id}/run", response_model=None)
async def run_workflow_now(
    workflow_id: str, current_user: CurrentUserDep, uow: UowDep,
    redirect: bool = False,
) -> dict | RedirectResponse:
    workflow = await _manageable_workflow(workflow_id, current_user, uow)
    run = await _workflow_service().run(
        workflow,
        uow,
        {
            "channel_id": workflow.channel_id,
            "initiated_by": current_user["id"],
            "text": "",
        },
        trigger_type="manual",
    )
    if redirect:
        target = f"/workflows/{workflow.id}" if redirect == "detail" else "/workflows"
        return RedirectResponse(target, status_code=303)
    return _run_json(run)


@router.post("/{workflow_id}/runs/{run_id}/retry", response_model=None)
async def retry_workflow_run(
    workflow_id: str, run_id: str,
    current_user: CurrentUserDep, uow: UowDep,
    redirect: bool = False,
) -> dict | RedirectResponse:
    workflow = await _manageable_workflow(workflow_id, current_user, uow)
    failed = await uow.workflows.get_run(run_id)
    if failed is None or failed.workflow_id != workflow.id:
        raise HTTPException(status_code=404, detail="Workflow run not found")
    try:
        run = await _workflow_service().retry_failed(
            workflow, failed, uow, initiated_by=current_user["id"]
        )
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    if redirect:
        return RedirectResponse(f"/workflows/{workflow.id}", status_code=303)
    return _run_json(run)


async def _set_workflow_enabled(workflow_id: str, enabled: bool, user: dict, uow):
    workflow = await _manageable_workflow(workflow_id, user, uow)
    workflow.enabled = enabled
    await uow.workflows.update(workflow)
    await uow.commit()
    return RedirectResponse("/workflows", status_code=303)


@router.post("/{workflow_id}/enable")
async def enable_workflow(
    workflow_id: str, current_user: CurrentUserDep, uow: UowDep,
) -> RedirectResponse:
    return await _set_workflow_enabled(workflow_id, True, current_user, uow)


def _run_audit_doc(workflow, run) -> dict:
    return {
        "workflow_id": workflow.id,
        "workflow_name": workflow.name,
        "run_id": run.id,
        "trigger_type": run.trigger_type,
        "status": run.status.value,
        "started_at": run.started_at.isoformat(),
        "finished_at": run.finished_at.isoformat() if run.finished_at else None,
        "error": run.error,
        "trigger_payload": run.event,
        "steps": [
            {
                "step_id": result["step_id"],
                "status": result["status"],
                "output": result.get("output"),
            }
            for result in run.step_results
        ],
        "lineage": {
            "attempt": run.attempt,
            "parent_run_id": run.parent_run_id,
            "root_run_id": run.root_run_id,
            "retry_initiated_by": run.retry_initiated_by,
        },
    }


@router.get("/{workflow_id}/runs/{run_id}/export")
async def export_workflow_run(
    workflow_id: str, run_id: str,
    current_user: CurrentUserDep, uow: UowDep,
    format: str = "json",
) -> Response:
    workflow = await _manageable_workflow(workflow_id, current_user, uow)
    run = await uow.workflows.get_run(run_id)
    if run is None or run.workflow_id != workflow.id:
        raise HTTPException(status_code=404, detail="Workflow run not found")
    doc = _run_audit_doc(workflow, run)
    if format == "csv":
        buffer = io.StringIO()
        writer = csv.writer(buffer)
        writer.writerow(["step_id", "status", "workflow_id", "run_id"])
        for step in doc["steps"]:
            writer.writerow(
                [step["step_id"], step["status"], doc["workflow_id"], doc["run_id"]]
            )
        return Response(
            content=buffer.getvalue(),
            media_type="text/csv",
            headers={
                "Content-Disposition": (
                    f'attachment; filename="workflow_{workflow.id}_run_{run.id}.csv"'
                )
            },
        )
    return JSONResponse(doc)


@router.post("/{workflow_id}/disable")
async def disable_workflow(
    workflow_id: str, current_user: CurrentUserDep, uow: UowDep,
) -> RedirectResponse:
    return await _set_workflow_enabled(workflow_id, False, current_user, uow)


@router.get("/{workflow_id}/delete", response_class=HTMLResponse)
async def delete_workflow_page(
    workflow_id: str, request: Request,
    current_user: CurrentUserDep, uow: UowDep,
) -> Response:
    workflow = await _manageable_workflow(workflow_id, current_user, uow)
    context = await _page_context(request, current_user, uow)
    return templates.TemplateResponse(
        request=request, name="workflow_delete.html",
        context={**context, "workflow": workflow},
    )


@router.post("/{workflow_id}/delete")
async def delete_workflow(
    workflow_id: str, current_user: CurrentUserDep, uow: UowDep,
    confirmation: str = Form(...),
) -> RedirectResponse:
    workflow = await _manageable_workflow(workflow_id, current_user, uow)
    if confirmation.strip() != workflow.name:
        raise HTTPException(status_code=422, detail="Confirmation name does not match")
    await uow.workflows.delete(workflow.id)
    await uow.commit()
    return RedirectResponse("/workflows", status_code=303)


@router.get("/{workflow_id}/runs")
async def workflow_runs(workflow_id: str, current_user: CurrentUserDep, uow: UowDep) -> list[dict]:
    workflow = await uow.workflows.get(workflow_id)
    if workflow is None:
        raise HTTPException(status_code=404, detail="Workflow not found")
    if not await uow.channels.can_member_access(workflow.channel_id, current_user["id"]):
        raise HTTPException(status_code=403, detail="You cannot view this workflow")
    return [_run_json(run) for run in await uow.workflows.list_runs(workflow_id)]
