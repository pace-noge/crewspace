"""Database backend contracts.

Set CREWSPACE_TEST_POSTGRES_URL to an isolated disposable PostgreSQL database to
run the live PostgreSQL contract. The default test suite remains self-contained.
"""
from __future__ import annotations

import os

import pytest
from sqlalchemy.engine import make_url

from crewspace.config import Settings
from crewspace.infrastructure.db import Database


@pytest.mark.skipif(
    not os.getenv("CREWSPACE_TEST_POSTGRES_URL"),
    reason="CREWSPACE_TEST_POSTGRES_URL is not configured",
)
async def test_postgresql_database_contract():
    database_url = os.environ["CREWSPACE_TEST_POSTGRES_URL"]
    url = make_url(database_url)
    assert url.get_backend_name() == "postgresql"
    assert url.database and "test" in url.database.lower(), (
        "PostgreSQL contract requires an isolated database whose name contains 'test'"
    )

    database = await Database.create(
        Settings(database_url=database_url, seed_admin_password="postgres-contract")
    )
    try:
        async with database.uow() as uow:
            member = await uow.auth.get_member("user_bilal")
            assert member is not None
            assert member["name"] == "Bilal"

            board = await uow.boards.get_board("board_main")
            assert board is not None

            revision = await (
                await uow._conn.execute("SELECT version_num FROM alembic_version")
            ).fetchone()
            assert revision is not None
    finally:
        await database.close()
