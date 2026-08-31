"""M9.3 — Health / readiness endpoint router.

Liveness (`/health`) never touches the database; readiness (`/ready`) verifies
that a DB connection is reachable and that migrations are at head. The router
is deliberately thin: it reaches the database through `request.app.state.db`
and uses only connection methods (`exec_driver_sql`) — it never imports
infrastructure or domain models, keeping it a pure API seam.
"""
from __future__ import annotations

import logging
from pathlib import Path

from alembic.config import Config
from alembic.script import ScriptDirectory
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

router = APIRouter()
_logger = logging.getLogger("crewspace.health")


def _get_expected_head() -> str:
    """Return the current Alembic head revision string."""
    # health.py is at src/crewspace/api/routers/ -> parents[4] is the repo root.
    root = Path(__file__).resolve().parents[4]
    config = Config(str(root / "alembic.ini"))
    config.set_main_option("script_location", str(root / "migrations"))
    config.attributes["configure_logger"] = False
    script_dir = ScriptDirectory.from_config(config)
    head = script_dir.get_current_head()
    if not head:
        raise RuntimeError("deployed Alembic head is unavailable")
    return head


@router.get("/health")
def health() -> dict[str, str]:
    """Liveness probe — no DB touch."""
    return {"status": "ok"}


@router.get("/ready")
async def ready(request: Request) -> JSONResponse:
    """Readiness probe: DB reachable + migrations at head."""
    db = getattr(request.app.state, "db", None)
    if db is None:
        return JSONResponse(
            status_code=503,
            content={
                "status": "not_ready",
                "database": "error",
                "detail": "database is not initialized",
                "migrations": "unknown",
            },
        )
    # Step 1: verify database connectivity.
    try:
        async with db.engine.connect() as conn:
            await conn.exec_driver_sql("SELECT 1")
    except Exception as exc:
        _logger.warning("readiness check failed (database): %s", exc)
        return JSONResponse(
            status_code=503,
            content={
                "status": "not_ready",
                "database": "error",
                "detail": "database check failed",
                "migrations": "unknown",
            },
        )

    # Step 2: resolve the migration target from the deployed application.
    try:
        expected = _get_expected_head()
        if not expected:
            raise RuntimeError("deployed Alembic head is unavailable")
    except Exception as exc:
        _logger.warning("readiness check failed (migration metadata): %s", exc)
        return JSONResponse(
            status_code=503,
            content={
                "status": "not_ready",
                "database": "ok",
                "detail": "migration metadata unavailable",
                "migrations": "unknown",
            },
        )

    # Step 3: verify the database schema is at that head.
    try:
        async with db.engine.connect() as conn:
            raw = await conn.exec_driver_sql(
                "SELECT version_num FROM alembic_version ORDER BY version_num"
            )
            revisions = list(raw.scalars().all())
    except Exception as exc:
        _logger.warning("readiness check failed (migration query): %s", exc)
        return JSONResponse(
            status_code=503,
            content={
                "status": "not_ready",
                "database": "ok",
                "detail": "migration query failed",
                "migrations": "unknown",
            },
        )

    if revisions == [expected]:
        return JSONResponse(
            status_code=200,
            content={
                "status": "ready",
                "database": "ok",
                "migrations": "head",
                "revision": expected,
                "expected_revision": expected,
            },
        )

    _logger.warning(
        "readiness check failed (migration revisions): current=%s expected=%s",
        revisions,
        expected,
    )
    return JSONResponse(
        status_code=503,
        content={
            "status": "not_ready",
            "database": "ok",
            "migrations": "behind",
            "detail": "database migration revisions do not match deployed head",
            "revisions": revisions,
            "expected_revision": expected,
        },
    )
