from __future__ import annotations

import asyncio
from logging.config import fileConfig

from alembic import context
from sqlalchemy import pool
from sqlalchemy.ext.asyncio import async_engine_from_config

from crewspace.config import Settings
from crewspace.infrastructure.models import Base

config = context.config
if config.config_file_name is not None and config.attributes.get("configure_logger", True):
    fileConfig(config.config_file_name)
if not config.get_main_option("sqlalchemy.url"):
    database_url = Settings().database_url
    assert database_url is not None
    config.set_main_option("sqlalchemy.url", database_url.replace("%", "%%"))

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    context.configure(
        url=config.get_main_option("sqlalchemy.url"),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
        render_as_batch=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection) -> None:
    dialect_name = connection.dialect.name
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        # SQLite can't reliably introspect these (unbounded String == TEXT,
        # NOT NULL / check constraints / FK ondelete aren't reflected back), so
        # comparing them only produces false-positive drift. PostgreSQL keeps
        # the strict checks.
        compare_type=dialect_name != "sqlite",
        compare_nullable=dialect_name != "sqlite",
        compare_constraints=dialect_name != "sqlite",
        compare_server_default=False,
        render_as_batch=dialect_name == "sqlite",
    )
    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    connectable = async_engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)
    await connectable.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    asyncio.run(run_async_migrations())
