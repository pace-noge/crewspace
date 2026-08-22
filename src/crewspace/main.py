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
from .api.routers import agents, auth, boards, cards, chat, cronjobs, pages, teams, tools, workflows
from .application.scheduling import SchedulerLoop
from .application.workflows import WorkflowSchedulerLoop
from .api.connection import manager
from .dto.mappers import to_message
from .security import is_same_origin
from .infrastructure.db import logger as db_logger

_STATIC_DIR = os.path.join(os.path.dirname(__file__), "static")


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    app.state.settings = settings
    # Allow tests to inject a pre-configured Database (e.g., against a temp file)
    if not hasattr(app.state, "db"):
        db = await Database.create(settings)
        app.state.db = db
    else:
        db = app.state.db
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
        await db.close()


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
    app.include_router(cronjobs.router)
    app.include_router(tools.router)
    app.include_router(workflows.router)
    app.include_router(workflows.hooks_router)
    return app


app = create_app()
