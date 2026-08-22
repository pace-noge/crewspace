"""Test fixtures for the layered architecture.

We build the app, open a Database against a temp sqlite file, seed it, and attach
it to app.state.db (exactly what the lifespan does). `client` drives HTTP + WS
in-process; TestClient runs the app in its own event loop, and all DB access
goes through the HTTP layer, so there's a single consistent loop per request.
pytest-asyncio is in auto mode.
"""
from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from crewspace.main import create_app

from crewspace.config import Settings
from crewspace.infrastructure.db import Database


@pytest.fixture
def app():
    tmp = Path(tempfile.mkdtemp())
    settings = Settings(db_path=str(tmp / "test.db"))
    database = asyncio.run(Database.create(settings))
    application = create_app()
    application.state.db = database
    application.state.settings = settings
    application.state.start_schedulers = False
    yield application
    # TestClient runs lifespan and closes the injected DB on its serving loop.
    # App-only tests never enter lifespan, so the fixture closes their DB here.
    if not getattr(application.state, "db_closed_by_lifespan", False):
        asyncio.run(database.close())


@pytest.fixture
def anonymous_client(app):
    with TestClient(app) as c:
        yield c


@pytest.fixture
def client(anonymous_client):
    anonymous_client.headers["Origin"] = "http://testserver"
    response = anonymous_client.post(
        "/auth/login", data={"username": "Bilal", "password": "admin123"}
    )
    assert response.status_code == 200
    yield anonymous_client
