"""M9.3 — Health and readiness endpoint acceptance tests."""
from __future__ import annotations

import ast
import subprocess
import sys
from pathlib import Path

from fastapi.testclient import TestClient

from crewspace.main import create_app


def test_health_is_live_without_database() -> None:
    """Liveness never touches the DB — it answers before lifespan starts."""
    app = create_app()
    client = TestClient(app)  # no context manager: lifespan/DB not started
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_ready_reports_database_and_migration_head(client) -> None:
    response = client.get("/ready")
    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "ready"
    assert payload["database"] == "ok"
    assert payload["migrations"] == "head"


def test_ready_returns_503_before_database_is_initialized() -> None:
    """Readiness fails closed if lifespan has not installed app.state.db."""
    app = create_app()
    client = TestClient(app)  # no lifespan and no injected DB
    response = client.get("/ready")
    assert response.status_code == 503
    payload = response.json()
    assert payload["status"] == "not_ready"
    assert payload["database"] == "error"


def test_ready_returns_503_when_database_unavailable() -> None:
    """Simulate a dead DB: engine.connect() raises an exception."""

    class _BrokenEngine:
        def connect(self):
            raise RuntimeError("database unavailable")

    app = create_app()
    app.state.db = type("BrokenDb", (), {"engine": _BrokenEngine()})()
    client = TestClient(app)  # skip lifespan; use injected broken DB
    response = client.get("/ready")
    assert response.status_code == 503
    payload = response.json()
    assert payload["status"] == "not_ready"
    assert payload["database"] == "error"
    assert payload["detail"] == "database check failed"
    assert "database unavailable" not in response.text


def test_ready_returns_503_when_migration_metadata_is_unavailable(monkeypatch) -> None:
    from crewspace.api.routers import health

    def _broken_head() -> str:
        raise RuntimeError("migration directory unavailable")

    monkeypatch.setattr(health, "_get_expected_head", _broken_head)
    app = create_app()

    class _Result:
        def scalar_one_or_none(self):
            return None

    class _Connection:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        async def exec_driver_sql(self, _sql: str):
            return _Result()

    class _Engine:
        def connect(self):
            return _Connection()

    app.state.db = type("Db", (), {"engine": _Engine()})()
    response = TestClient(app).get("/ready")

    assert response.status_code == 503
    payload = response.json()
    assert payload["status"] == "not_ready"
    assert payload["database"] == "ok"
    assert payload["migrations"] == "unknown"
    assert payload["detail"] == "migration metadata unavailable"
    assert "migration directory unavailable" not in response.text


def test_ready_returns_503_when_migration_query_fails() -> None:
    class _Result:
        def scalar_one_or_none(self):
            return None

    class _Connection:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        async def exec_driver_sql(self, sql: str):
            if "alembic_version" in sql:
                raise RuntimeError("relation alembic_version missing")
            return _Result()

    class _Engine:
        def connect(self):
            return _Connection()

    app = create_app()
    app.state.db = type("Db", (), {"engine": _Engine()})()
    response = TestClient(app).get("/ready")

    assert response.status_code == 503
    payload = response.json()
    assert payload["status"] == "not_ready"
    assert payload["database"] == "ok"
    assert payload["migrations"] == "unknown"
    assert payload["detail"] == "migration query failed"
    assert "alembic_version missing" not in response.text


def test_ready_returns_503_when_deployed_head_is_empty(monkeypatch) -> None:
    from crewspace.api.routers import health

    monkeypatch.setattr(health, "_get_expected_head", lambda: "")
    app = create_app()

    class _Result:
        def scalars(self):
            return self

        def all(self):
            return []

    class _Connection:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        async def exec_driver_sql(self, _sql: str):
            return _Result()

    class _Engine:
        def connect(self):
            return _Connection()

    app.state.db = type("Db", (), {"engine": _Engine()})()
    response = TestClient(app).get("/ready")

    assert response.status_code == 503
    payload = response.json()
    assert payload["status"] == "not_ready"
    assert payload["migrations"] == "unknown"
    assert payload["detail"] == "migration metadata unavailable"


def test_ready_returns_503_when_database_has_multiple_revisions(monkeypatch) -> None:
    from crewspace.api.routers import health

    monkeypatch.setattr(health, "_get_expected_head", lambda: "head_revision")
    app = create_app()

    class _Result:
        def scalars(self):
            return self

        def all(self):
            return ["head_revision", "other_revision"]

    class _Connection:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        async def exec_driver_sql(self, _sql: str):
            return _Result()

    class _Engine:
        def connect(self):
            return _Connection()

    app.state.db = type("Db", (), {"engine": _Engine()})()
    response = TestClient(app).get("/ready")

    assert response.status_code == 503
    payload = response.json()
    assert payload["status"] == "not_ready"
    assert payload["migrations"] == "behind"
    assert payload["revisions"] == ["head_revision", "other_revision"]
    assert payload["expected_revision"] == "head_revision"


def test_ready_returns_503_when_migrations_are_behind() -> None:
    class _Result:
        def __init__(self, revisions: list[str] | None = None):
            self.revisions = revisions or []

        def scalars(self):
            return self

        def all(self):
            return self.revisions

    class _Connection:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        async def exec_driver_sql(self, sql: str):
            if "alembic_version" in sql:
                return _Result(["old_revision"])
            return _Result()

    class _Engine:
        def connect(self):
            return _Connection()

    app = create_app()
    app.state.db = type("BehindDb", (), {"engine": _Engine()})()
    response = TestClient(app).get("/ready")

    assert response.status_code == 503
    payload = response.json()
    assert payload["status"] == "not_ready"
    assert payload["database"] == "ok"
    assert payload["migrations"] == "behind"
    assert payload["revisions"] == ["old_revision"]
    assert payload["expected_revision"]


def test_health_module_has_no_sqlalchemy_imports() -> None:
    """Router remains an API seam; DB details are reached via app.state.db."""
    module = Path(__file__).resolve().parents[1] / "src" / "crewspace" / "api" / "routers" / "health.py"
    tree = ast.parse(module.read_text())
    imports: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imports.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imports.add(node.module)
    assert not any("sqlalchemy" in name for name in imports), f"health.py imports sqlalchemy: {imports}"


def test_makemigrations_still_clean() -> None:
    """Migration compat guard: health endpoint added no DB schema changes."""
    result = subprocess.run(
        [sys.executable, "-m", "crewspace.management.cli", "makemigrations", "--check"],
        capture_output=True, text=True, timeout=120,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "No changes detected" in result.stdout
