"""Crewspace — a shared operational workspace for humans and AI agents."""
from __future__ import annotations

import uvicorn

from .config import get_settings
from .main import app, create_app

__all__ = ["app", "create_app", "main"]


def main() -> None:
    settings = get_settings()
    uvicorn.run(
        "crewspace.main:app",
        host=settings.host,
        port=settings.port,
        reload=False,
    )
