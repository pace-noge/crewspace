"""Application entrypoint (Web API).

Assembles the FastAPI app: lifespan opens the Database and stores it on
app.state.db; routers are mounted; dependency injection bridges the web layer to
the application/infrastructure layers. No business logic lives here.
"""
from __future__ import annotations

import os
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from .config import get_settings
from .infrastructure.db import Database
from .infrastructure.mcp_client import ExternalMcpToolExecutor, build_external_tool_executor
from .infrastructure.workflow_webhooks import build_workflow_webhook_executor
from .api.routers import (agents, auth, boards, cards, change_sets, chat, coding_runs,
                            cronjobs, inbox, pages, presence, teams, tools, workflows)
from .application.scheduling import SchedulerLoop
from .application.workflows import WorkflowSchedulerLoop
from .application.coding_runs import reconcile_interrupted_runs
from .application.inbox_store import inbox_store
from .application.inbox_events import inbox_events
from .api.connection import agent_manager, manager, thread_manager
from .dto.mappers import to_message
from .logging_config import configure_logging, format_access_line
from .security import is_same_origin
from .infrastructure.db import logger as db_logger

_STATIC_DIR = os.path.join(os.path.dirname(__file__), "static")

import logging
import time as _time
from uuid import uuid4

_access_logger = logging.getLogger("crewspace.access")


def _request_id(request: Request) -> str:
    """Return the client-provided request id if present, else a fresh one."""
    return request.headers.get("x-request-id") or uuid4().hex[:12]




def _make_agent_disconnect_reconciler(db):
    """Return a disconnect callback that interrupts an agent's in-flight runs."""

    async def _reconcile(agent_id: str) -> None:
        async with db.uow() as uow:
            await reconcile_interrupted_runs(uow, agent_id=agent_id)

    return _reconcile


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    app.state.settings = settings
    configure_logging(settings)
    # Allow tests to inject a pre-configured Database (e.g., against a temp file).
    if not hasattr(app.state, "db"):
        db = await Database.create(settings)
        app.state.db = db
    else:
        db = app.state.db
    # On startup, reconcile any runs left in-flight by a previous process or a
    # dropped agent connection into the interrupted terminal state.
    async with db.uow() as bootstrap_uow:
        await reconcile_interrupted_runs(bootstrap_uow)
    agent_manager.on_disconnect = _make_agent_disconnect_reconciler(db)
    # Tests that inject a pre-configured Database (e.g. a temp file) drive
    # requests directly and do not need the background pollers running; leaving
    # them on contends with every request for the single SQLite file (WAL +
    # busy timeout) and makes the suite slow/flaky. Opt out via the fixture.
    start_schedulers = getattr(app.state, "start_schedulers", True)
    scheduler = SchedulerLoop(
        db, settings, mcp_executor_factory=build_external_tool_executor
    )
    async def broadcast_workflow_message(message):
        await manager.broadcast(
            message.channel_id, to_message(message).model_dump(mode="json")
        )

    async def broadcast_workflow_progress(event):
        channel_id = event.get("channel_id")
        if channel_id:
            await manager.broadcast(channel_id, event)

    workflow_scheduler = WorkflowSchedulerLoop(
        db,
        on_message=broadcast_workflow_message,
        on_progress=broadcast_workflow_progress,
        webhook_executor=build_workflow_webhook_executor(),
        mcp_executor=ExternalMcpToolExecutor(),
    )
    if start_schedulers:
        scheduler.start()
        workflow_scheduler.start()
    try:
        yield
    finally:
        await scheduler.stop()
        await workflow_scheduler.stop()
        manager.reset()
        thread_manager.reset()
        agent_manager.reset()
        inbox_store.reset()
        inbox_events.reset()
        await db.close()
        app.state.db_closed_by_lifespan = True


def create_app() -> FastAPI:
    app = FastAPI(title="Crewspace", version="0.2.0", lifespan=lifespan)

    @app.middleware("http")
    async def enforce_same_origin(request: Request, call_next):
        if request.method not in {"GET", "HEAD", "OPTIONS", "TRACE"}:
            origin = request.headers.get("origin")
            has_session = "crewspace_session" in request.cookies
            if (origin and not is_same_origin(origin, str(request.base_url))) or (
                has_session and not origin
            ):
                return JSONResponse({"detail": "Cross-origin request rejected"}, status_code=403)
        return await call_next(request)

    @app.middleware("http")
    async def access_log(request: Request, call_next):
        """Log method/path/status/duration + a correlation request id (M9.1)."""
        request_id = _request_id(request)
        started = _time.monotonic()
        try:
            response = await call_next(request)
        except Exception:
            # An unhandled exception surfaces as a 500 to the client; log the
            # access line with that status so every request is covered (M9.1).
            _access_logger.warning(
                format_access_line(
                    method=request.method,
                    path=request.url.path,
                    status=500,
                    duration_ms=(_time.monotonic() - started) * 1000,
                    request_id=request_id,
                )
            )
            raise
        duration_ms = (_time.monotonic() - started) * 1000
        _access_logger.info(
            format_access_line(
                method=request.method,
                path=request.url.path,
                status=response.status_code,
                duration_ms=duration_ms,
                request_id=request_id,
            )
        )
        return response

    # Vendor HTMX locally so the UI works without external CDN access
    # (corporate networks / offline UAT often block unpkg.com).
    app.mount("/static", StaticFiles(directory=_STATIC_DIR), name="static")
    app.include_router(agents.router)
    app.include_router(auth.router)
    app.include_router(chat.router)
    app.include_router(boards.router)
    app.include_router(cards.router)
    app.include_router(pages.router)
    app.include_router(teams.router)
    app.include_router(change_sets.router)
    app.include_router(coding_runs.router)
    app.include_router(inbox.router)
    app.include_router(cronjobs.router)
    app.include_router(tools.router)
    app.include_router(workflows.router)
    app.include_router(workflows.hooks_router)
    app.include_router(presence.router)
    return app


app = create_app()
