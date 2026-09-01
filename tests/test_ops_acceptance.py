"""M9.7 — cohesive ops acceptance suite.

One bounded test file exercising the key invariant from every M9 slice. Each
test is independent so the suite can be run as a standalone ops smoke check.

M9.1  Structured logging      — JSON/text formatter honours env knobs.
M9.2  Config validation        — non-loopback bind refused with dev credentials.
M9.3  Health / readiness       — /health is live, /ready reflects migration head.
M9.4  Containerization        — non-root image, persistent Compose, /ready healthcheck.
M9.5  Backup / restore        — real CLI round trip: backup → mutate → restore.
M9.6  Runbook docs            — env names in DEPLOYMENT.md are valid Settings fields.

Reuses the package-level `conftest` fixtures (`client`, `app`) which initialise
a real temp SQLite database at the current Alembic head, matching established
Crewspace test conventions.
"""
from __future__ import annotations

import ast
import json
import logging
import re
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Generator

import pytest
import yaml

from crewspace.config import Settings
from crewspace.main import create_app

ROOT = Path(__file__).resolve().parents[1]
DEPLOYMENT_DOC = ROOT / "docs" / "DEPLOYMENT.md"

from fastapi.testclient import TestClient  # noqa: E402


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _settings_env_names() -> set[str]:
    """Return every CREWSPACE_<FIELD> name Settings accepts."""
    src = (ROOT / "src" / "crewspace" / "config.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == "Settings":
            fields = {
                target.id
                for item in node.body
                if isinstance(item, ast.AnnAssign) and isinstance(item.target, ast.Name)
                for target in [item.target]
            }
            return {f"CREWSPACE_{f.upper()}" for f in fields}
    raise AssertionError("Settings class not found in config.py")


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _run_manage(*args: str, db_url: str | None = None) -> subprocess.CompletedProcess:
    import os

    env = dict(os.environ)
    env.pop("CREWSPACE_DATABASE_URL", None)
    if db_url:
        env["CREWSPACE_DATABASE_URL"] = db_url
    return subprocess.run(
        [sys.executable, "-m", "crewspace.management.cli", *args],
        capture_output=True,
        text=True,
        cwd=str(ROOT),
        env=env,
        timeout=60,
    )


# ---------------------------------------------------------------------------
# M9.1 — Structured logging
# ---------------------------------------------------------------------------


def test_m9p1_json_formatter_emits_valid_json() -> None:
    from crewspace.logging_config import StructuredFormatter

    formatter = StructuredFormatter("json")
    record = logging.LogRecord(
        name="crewspace.ops",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg="test message",
        args=(),
        exc_info=None,
    )
    parsed = json.loads(formatter.format(record))
    assert parsed["levelname"] == "INFO"
    assert parsed["name"] == "crewspace.ops"
    assert "message" in parsed


def test_m9p1_text_formatter_includes_level_and_extra() -> None:
    from crewspace.logging_config import StructuredFormatter

    formatter = StructuredFormatter("text")
    record = logging.LogRecord(
        name="crewspace.ops",
        level=logging.WARNING,
        pathname=__file__,
        lineno=1,
        msg="oops",
        args=(),
        exc_info=None,
    )
    output = formatter.format(record)
    assert "oops" in output
    assert "level=WARNING" in output
    assert "logger=crewspace.ops" in output


def test_m9p1_access_line_builder_is_grepable() -> None:
    from crewspace.logging_config import format_access_line

    line = format_access_line(
        method="GET", path="/health", status=200,
        duration_ms=1.5, request_id="req-test-001",
    )
    assert "method=GET" in line
    assert "path=/health" in line
    assert "status=200" in line
    assert "request_id=req-test-001" in line


def test_m9p1_settings_expose_logging_knobs() -> None:
    s = Settings(log_level="DEBUG", log_format="json", log_json=True)
    assert s.log_level == "DEBUG"
    assert s.log_format == "json"
    assert s.log_json is True
    d = Settings()
    assert d.log_level == "INFO"
    assert d.log_format == "text"
    assert d.log_json is False


def test_m9p1_configure_logging_honours_mode() -> None:
    import io
    from crewspace.logging_config import configure_logging

    stream = io.StringIO()
    capture = logging.StreamHandler(stream)
    handler = configure_logging(
        Settings(log_level="INFO", log_format="json"),
        handlers=[capture],
    )
    assert handler is not None
    logger = logging.getLogger("crewspace.ops.acceptance.test")
    logger.addHandler(capture)
    previous_level = logger.level
    previous_propagate = logger.propagate
    try:
        logger.setLevel(logging.INFO)
        logger.propagate = False
        logger.info("structured msg")
        assert "structured msg" in stream.getvalue()
        # JSON mode must emit a parseable single object.
        parsed = json.loads(stream.getvalue().strip())
        assert parsed["levelname"] == "INFO"
    finally:
        logger.removeHandler(capture)
        logger.setLevel(previous_level)
        logger.propagate = previous_propagate


# ---------------------------------------------------------------------------
# M9.2 — Config validation
# ---------------------------------------------------------------------------


def test_m9p2_non_loopback_with_dev_secret_fails() -> None:
    with pytest.raises(ValueError, match="Set CREWSPACE_SECRET"):
        Settings(host="0.0.0.0", secret="dev-insecure-change-me")


def test_m9p2_non_loopback_with_dev_admin_password_fails() -> None:
    with pytest.raises(ValueError):
        Settings(host="0.0.0.0", seed_admin_password="admin123")


def test_m9p2_production_shaped_settings_pass() -> None:
    s = Settings(
        host="0.0.0.0",
        secret="a-real-random-secret-at-least-32-chars",
        seed_admin_password="a-real-admin-password",
    )
    assert s.host == "0.0.0.0"


def test_m9p2_invalid_db_backend_rejected() -> None:
    with pytest.raises(ValueError, match="backend"):
        Settings(database_url="mysql+pymysql://root@localhost/cw")


def test_m9p2_port_range_validated() -> None:
    with pytest.raises(ValueError, match="port"):
        Settings(port=0)


# ---------------------------------------------------------------------------
# M9.3 — Health / readiness probes
# ---------------------------------------------------------------------------


def test_m9p3_health_is_live_without_database() -> None:
    app = create_app()
    client = TestClient(app)  # no lifespan / DB
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_m9p3_ready_reports_migration_head(client) -> None:
    """/ready is 200 with the migrated temp DB reporting migrations=head and exact revision."""
    from crewspace.api.routers.health import _get_expected_head

    expected = _get_expected_head()
    response = client.get("/ready")
    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "ready"
    assert payload["database"] == "ok"
    assert payload["migrations"] == "head"
    assert payload["revision"] == expected
    assert payload["expected_revision"] == expected


# ---------------------------------------------------------------------------
# M9.4 — Containerization
# ---------------------------------------------------------------------------


def test_m9p4_dockerfile_is_multistage_non_root() -> None:
    dockerfile = _read(ROOT / "Dockerfile")
    from_lines = re.findall(r"(?mi)^FROM\s+\S+", dockerfile)
    assert len(from_lines) >= 2, "must be multi-stage"
    assert "python:3.14-slim" in dockerfile
    users = re.findall(r"(?mi)^USER\s+(\S+)", dockerfile)
    assert users and users[-1] not in {"root", "0", "0:0"}, "must not run as root"


def test_m9p4_compose_app_is_non_root_persistent_with_healthcheck() -> None:
    compose = yaml.safe_load(_read(ROOT / "docker-compose.yml"))
    app = compose["services"]["app"]
    assert str(app["user"]) not in {"0", "root", "0:0"}
    assert any("/app/data" in vol for vol in app["volumes"])
    health_test = " ".join(app["healthcheck"]["test"])
    assert "/ready" in health_test


def test_m9p4_compose_postgres_profile_is_optional() -> None:
    compose = yaml.safe_load(_read(ROOT / "docker-compose.yml"))
    db = compose["services"]["db"]
    assert "postgres" in db.get("profiles", [])


# ---------------------------------------------------------------------------
# M9.5 — Backup / restore
# ---------------------------------------------------------------------------


def test_m9p5_backup_produces_integrity_clean_snapshot(tmp_path: Path) -> None:
    db = tmp_path / "backup_smoke.db"
    db_url = f"sqlite+aiosqlite:///{db}"

    conn = sqlite3.connect(str(db))
    conn.execute("CREATE TABLE member (id TEXT PRIMARY KEY, name TEXT)")
    conn.execute("INSERT INTO member VALUES ('test-ops-1', 'ops smoke')")
    conn.commit()
    conn.close()

    out = tmp_path / "smoke-backup.db"
    result = _run_manage("backup", "--out", str(out), db_url=db_url)
    assert result.returncode == 0, f"backup failed: {result.stderr}"

    check = sqlite3.connect(str(out))
    integrity = check.execute("PRAGMA integrity_check").fetchone()[0]
    value = check.execute("SELECT name FROM member WHERE id='test-ops-1'").fetchone()
    check.close()

    assert integrity == "ok"
    assert value is not None and value[0] == "ops smoke"


def test_m9p5_restore_round_trip_preserves_data(tmp_path: Path) -> None:
    db = tmp_path / "round_trip.db"
    db_url = f"sqlite+aiosqlite:///{db}"
    backup = str(tmp_path / "round-trip-backup.db")

    conn = sqlite3.connect(str(db))
    conn.execute("CREATE TABLE member (id TEXT PRIMARY KEY, name TEXT)")
    conn.execute("INSERT INTO member VALUES ('baseline', 'before')")
    conn.commit()
    conn.close()

    result = _run_manage("backup", "--out", backup, db_url=db_url)
    assert result.returncode == 0, f"backup failed: {result.stderr}"

    conn2 = sqlite3.connect(str(db))
    conn2.execute("DELETE FROM member")
    conn2.execute("INSERT INTO member VALUES ('mutant', 'after')")
    conn2.commit()
    conn2.close()

    result2 = _run_manage("restore", backup, db_url=db_url)
    assert result2.returncode == 0, f"restore failed: {result2.stderr}"

    conn3 = sqlite3.connect(str(db))
    rows = list(conn3.execute("SELECT id, name FROM member ORDER BY id"))
    conn3.close()

    assert ("baseline", "before") in rows, "original data must be restored"
    assert ("mutant", "after") not in rows, "mutated data must be gone"


def test_m9p5_backup_rejects_postgres_with_guidance(tmp_path: Path) -> None:
    result = _run_manage(
        "backup", "--out", str(tmp_path / "pg.db"),
        db_url="postgresql+asyncpg://u:pw@localhost/cw",
    )
    assert result.returncode == 1
    assert "pg_dump" in result.stderr or "psql" in result.stderr


# ---------------------------------------------------------------------------
# M9.6 — Runbook docs env-drift guard
# ---------------------------------------------------------------------------


def test_m9p6_deployment_doc_exists_and_covers_security_vars() -> None:
    assert DEPLOYMENT_DOC.exists(), "DEPLOYMENT.md must exist"
    text = _read(DEPLOYMENT_DOC)
    for var in (
        "CREWSPACE_SECRET",
        "CREWSPACE_SEED_ADMIN_PASSWORD",
        "CREWSPACE_LOG_LEVEL",
        "CREWSPACE_DATABASE_URL",
    ):
        assert var in text, f"{var} must be documented"


def test_m9p6_deployment_doc_warns_about_dev_defaults() -> None:
    text = _read(DEPLOYMENT_DOC).lower()
    assert "loopback" in text, "must warn about dev defaults on non-loopback"


def test_m9p6_env_names_in_deployment_doc_are_valid_settings_fields() -> None:
    text = _read(DEPLOYMENT_DOC)
    doc_names = {m.group(0) for m in re.finditer(r"CREWSPACE_[A-Z0-9_]+", text)}
    settings_names = _settings_env_names()
    drift = doc_names - settings_names
    assert not drift, f"DEPLOYMENT.md references unknown env names: {drift}"


def test_m9p6_releasing_doc_describes_versioning_and_verified_flow() -> None:
    releasing = ROOT / "docs" / "RELEASING.md"
    assert releasing.exists(), "RELEASING.md must exist"
    text = _read(releasing).lower()
    for keyword in ("version", "tag", "milestone", "verified"):
        assert keyword in text, f"RELEASING.md must describe {keyword}"
