"""Django-style management commands for Crewspace.

Run via the ``crewspace-manage`` console script, e.g.::

    crewspace-manage createsuperuser
    crewspace-manage changepassword Bilal

Commands receive parsed ``argparse`` args and a freshly opened
``SqlAlchemyConnection`` (already inside a transaction). They raise
``ManagementCommandError`` to print a clean, non-traceback error and exit non-zero.
"""
from __future__ import annotations

import asyncio
import sys

from ..config import Settings
from ..infrastructure.db import Database, SqlAlchemyConnection


class ManagementCommandError(Exception):
    """Raised by a command to abort with a clean message (no traceback)."""


async def _with_connection(coro_factory):
    """Open the configured DB, run ``coro_factory(conn)``, commit."""
    settings = Settings()
    db = await Database.create(settings)
    try:
        async with db.engine.connect() as raw:
            conn: SqlAlchemyConnection = SqlAlchemyConnection(raw)
            result = await coro_factory(conn)
            await raw.commit()
            return result
    finally:
        await db.close()


def run_async(coro_factory):
    """Execute a command. ``coro_factory(conn)`` returns the coroutine to run
    with a live connection; ``run_async`` handles open/commit/close and clean
    error exits."""
    try:
        return asyncio.run(_with_connection(coro_factory))
    except ManagementCommandError as exc:
        print(f"error: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
