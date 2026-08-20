"""Crewspace namespace and data-path compatibility contracts."""
from __future__ import annotations

import asyncio
import sqlite3

from crewspace.config import Settings
from crewspace.infrastructure.db import Database


def test_crewspace_is_the_only_public_runtime_namespace():
    settings = Settings()
    assert settings.app_name == "Crewspace"
    assert settings.db_path == "data/crewspace.db"
    assert settings.database_url == "sqlite+aiosqlite:///data/crewspace.db"
    assert settings.model_config["env_prefix"] == "CREWSPACE_"


def test_explicit_database_url_overrides_legacy_db_path():
    settings = Settings(
        db_path="ignored.db",
        database_url="postgresql+asyncpg://crewspace:secret@db/crewspace",
    )
    assert settings.database_url == "postgresql+asyncpg://crewspace:secret@db/crewspace"


def test_postgresql_models_and_engine_are_constructible():
    from sqlalchemy import create_mock_engine
    from sqlalchemy.dialects import postgresql
    from sqlalchemy.schema import CreateTable

    from crewspace.infrastructure.models import Base

    settings = Settings(database_url="postgresql+asyncpg://crewspace:secret@db/crewspace")
    database = Database(settings)
    try:
        assert database.engine.url.drivername == "postgresql+asyncpg"
        dialect = postgresql.dialect()
        compiled = [str(CreateTable(table).compile(dialect=dialect)) for table in Base.metadata.sorted_tables]
        assert len(compiled) == 20
        assert all("CREATE TABLE" in statement for statement in compiled)
        create_mock_engine("postgresql://", lambda *_args, **_kwargs: None)
    finally:
        import asyncio

        asyncio.run(database.close())



def test_database_uses_sqlalchemy_async_engine(tmp_path):
    async def create_and_inspect():
        settings = Settings(db_path=str(tmp_path / "sqlalchemy.db"))
        database = await Database.create(settings)
        try:
            assert database.engine.url.drivername == "sqlite+aiosqlite"
            async with database.uow() as uow:
                member = await uow.auth.get_member("user_bilal")
                assert member["name"] == "Bilal"
                revision = await (
                    await uow._conn.execute("SELECT version_num FROM alembic_version")
                ).fetchone()
                assert revision is not None
        finally:
            await database.close()

    import asyncio

    asyncio.run(create_and_inspect())


def test_legacy_member_role_rename_preserves_archived_at(tmp_path):
    target = tmp_path / "crewspace.db"
    with sqlite3.connect(target) as conn:
        conn.execute(
            """CREATE TABLE member (
                id TEXT PRIMARY KEY, kind TEXT NOT NULL,
                name TEXT NOT NULL, avatar TEXT, password_hash TEXT,
                role TEXT NOT NULL DEFAULT 'member',
                base_url TEXT, pubkey TEXT, backend TEXT NOT NULL DEFAULT 'stub',
                archived_at TEXT
            )"""
        )
        conn.execute(
            "INSERT INTO member (id, kind, name, role, backend, archived_at) "
            "VALUES ('m_old', 'human', 'Old Member', 'admin', 'stub', '2026-01-01T00:00:00+00:00')"
        )

    async def migrate_and_read() -> tuple[str, str | None]:
        settings = Settings(db_path=str(target))
        await Database.create(settings)
        with sqlite3.connect(target) as conn:
            row = conn.execute(
                "SELECT role, archived_at FROM member WHERE id='m_old'"
            ).fetchone()
            return row[0], row[1]

    role, archived_at = asyncio.run(migrate_and_read())
    assert role == "superadmin"  # legacy 'admin' was renamed
    assert archived_at == "2026-01-01T00:00:00+00:00"  # not silently dropped


def test_legacy_database_is_moved_to_crewspace_path(tmp_path):
    legacy = tmp_path / "agentic-kanban.db"
    target = tmp_path / "crewspace.db"
    with sqlite3.connect(legacy) as conn:
        conn.execute("CREATE TABLE migration_probe(value TEXT NOT NULL)")
        conn.execute("INSERT INTO migration_probe(value) VALUES ('preserved')")

    async def migrate_and_read() -> str:
        settings = Settings(db_path=str(target))
        await Database.create(settings)
        with sqlite3.connect(target) as conn:
            row = conn.execute("SELECT value FROM migration_probe").fetchone()
            assert row is not None
            return row[0]

    assert asyncio.run(migrate_and_read()) == "preserved"
    assert target.exists()
    assert not legacy.exists()
