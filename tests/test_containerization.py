"""M9.4 — portable OCI image and Compose deployment acceptance tests."""
from __future__ import annotations

import re
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]


def _read(name: str) -> str:
    return (ROOT / name).read_text()


def _compose() -> dict:
    return yaml.safe_load(_read("docker-compose.yml"))


def test_dockerfile_is_multistage_non_root_and_runs_crewspace() -> None:
    dockerfile = _read("Dockerfile")
    from_lines = re.findall(r"(?mi)^FROM\s+\S+", dockerfile)
    assert len(from_lines) >= 2
    assert "python:3.14-slim" in dockerfile
    users = re.findall(r"(?mi)^USER\s+(\S+)", dockerfile)
    assert users and users[-1] not in {"root", "0", "0:0"}
    assert re.search(r"(?mi)^EXPOSE\s+8000\b", dockerfile)
    assert '"uvicorn"' in dockerfile
    assert '"crewspace.main:app"' in dockerfile


def test_dockerfile_copies_runtime_migration_and_package_assets() -> None:
    dockerfile = _read("Dockerfile")
    for required in ("alembic.ini", "migrations", "src"):
        assert required in dockerfile
    assert 'PYTHONPATH="/app/src"' in dockerfile


def test_compose_app_is_non_root_persistent_and_health_checked() -> None:
    compose = _compose()
    app = compose["services"]["app"]
    assert app["build"] in (".", {"context": "."})
    assert str(app["user"]) not in {"0", "root", "0:0"}
    assert "8000:8000" in app["ports"]
    assert any("/app/data" in volume for volume in app["volumes"])
    health_test = " ".join(app["healthcheck"]["test"])
    assert "http://127.0.0.1:8000/ready" in health_test
    environment = app["environment"]
    assert environment["CREWSPACE_HOST"] == "0.0.0.0"
    assert environment["CREWSPACE_PORT"] == 8000
    assert "CREWSPACE_SECRET" in environment
    assert "CREWSPACE_SEED_ADMIN_PASSWORD" in environment


def test_compose_postgres_profile_is_optional_and_health_checked() -> None:
    db = _compose()["services"]["db"]
    assert "postgres" in db["image"]
    assert "postgres" in db["profiles"]
    assert db["healthcheck"]["test"]
    assert any("postgres" in volume for volume in db["volumes"])
    assert db["environment"]["POSTGRES_PASSWORD"] == "${POSTGRES_PASSWORD:-}"


def test_compose_app_defaults_to_sqlite_and_allows_database_url_override() -> None:
    app = _compose()["services"]["app"]
    database_url = app["environment"]["CREWSPACE_DATABASE_URL"]
    assert "CREWSPACE_DATABASE_URL" in database_url
    assert "sqlite+aiosqlite:////app/data/crewspace.db" in database_url


def test_dockerignore_excludes_secrets_vcs_and_runtime_data() -> None:
    ignored = set(_read(".dockerignore").splitlines())
    assert {".git", ".env", "data", ".venv", "__pycache__"} <= ignored


def test_readme_documents_docker_and_podman_commands() -> None:
    readme = _read("README.md")
    assert "docker compose up" in readme
    assert "podman compose up" in readme
    assert "CREWSPACE_SECRET" in readme
    assert "CREWSPACE_SEED_ADMIN_PASSWORD" in readme
    assert "/ready" in readme
