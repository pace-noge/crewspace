"""Regression tests for Django-style Crewspace management commands."""
from __future__ import annotations

import argparse
import asyncio

import pytest

from crewspace.management import ManagementCommandError
from crewspace.management.commands import (
    _run_changepassword,
    _run_createsuperuser,
    _run_makemigrations,
)


async def _create_superuser(app, monkeypatch, username: str, password: str) -> None:
    answers = iter([username])
    passwords = iter([password, password])
    monkeypatch.setattr("builtins.input", lambda _prompt: next(answers))
    monkeypatch.setattr("getpass.getpass", lambda _prompt: next(passwords))
    async with app.state.db.engine.connect() as raw:
        from crewspace.infrastructure.sql import SqlAlchemyConnection

        await _run_createsuperuser(
            argparse.Namespace(username=None), SqlAlchemyConnection(raw)
        )
        await raw.commit()


def test_createsuperuser_creates_login_capable_superadmin(app, monkeypatch):
    asyncio.run(_create_superuser(app, monkeypatch, "Recovery Admin", "recovery-pass"))

    async def verify():
        async with app.state.db.uow() as uow:
            member = await uow.auth.get_member_by_name("Recovery Admin")
            assert member is not None
            assert member["role"] == "superadmin"
            assert await uow.auth.verify_password(member["id"], "recovery-pass")

    asyncio.run(verify())


def test_createsuperuser_rejects_duplicate_name(app, monkeypatch):
    asyncio.run(_create_superuser(app, monkeypatch, "Recovery Admin", "first-pass"))

    with pytest.raises(ManagementCommandError, match="already exists"):
        asyncio.run(_create_superuser(app, monkeypatch, "Recovery Admin", "second-pass"))


def test_changepassword_updates_existing_account(app):
    async def reset_and_verify():
        from crewspace.infrastructure.sql import SqlAlchemyConnection

        async with app.state.db.engine.connect() as raw:
            conn = SqlAlchemyConnection(raw)
            await _run_changepassword(
                argparse.Namespace(
                    username="Bilal",
                    password="replacement-pass",
                    no_input=True,
                ),
                conn,
            )
            await raw.commit()

        async with app.state.db.uow() as uow:
            member = await uow.auth.get_member_by_name("Bilal")
            assert member is not None
            assert member["role"] == "superadmin"
            assert await uow.auth.verify_password(member["id"], "replacement-pass")
            assert not await uow.auth.verify_password(member["id"], "admin123")

    asyncio.run(reset_and_verify())


def test_makemigrations_check_passes_when_models_and_database_match(
    app, monkeypatch, capsys
):
    monkeypatch.setenv("CREWSPACE_DB_PATH", app.state.settings.db_path)

    _run_makemigrations(
        argparse.Namespace(check=True, name="auto", sql=False)
    )

    assert "Models are in sync" in capsys.readouterr().out


def test_makemigrations_check_detects_structural_model_drift(
    app, monkeypatch
):
    import sqlalchemy as sa

    from crewspace.infrastructure.models import Base

    monkeypatch.setenv("CREWSPACE_DB_PATH", app.state.settings.db_path)
    drift_table = sa.Table(
        "model_only_drift_table",
        Base.metadata,
        sa.Column("id", sa.String(), primary_key=True),
    )
    try:
        with pytest.raises(ManagementCommandError, match="schema drift detected"):
            _run_makemigrations(
                argparse.Namespace(check=True, name="auto", sql=False)
            )
    finally:
        Base.metadata.remove(drift_table)
