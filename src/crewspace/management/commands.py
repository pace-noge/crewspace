"""Management command implementations.

Each command is a callable that (a) registers its argparse subparser via
``register(parser)`` and (b) runs via ``run(args, conn)`` where ``conn`` is a
live ``SqlAlchemyConnection``. ``run`` is a coroutine.
"""
from __future__ import annotations

import datetime as dt
import getpass
import logging
import sys
import uuid
from pathlib import Path

from alembic.config import Config
from alembic.script import ScriptDirectory

from . import ManagementCommandError
from ..config import Settings
from ..infrastructure.models import Base
from ..infrastructure.repositories import SqlAlchemyAuthRepository
from ..security import hash_password
from .backup import register_backup, run_backup
from .restore import register_restore, run_restore


# Diff categories that represent a real, actionable migration on SQLite. Other
# categories (modify_nullable, add/remove_constraint, add/remove_fk) cannot be
# introspected reliably from a SQLite database, so they are ignored for drift
# detection to avoid false positives.
_SQLITE_MEANINGFUL = {
    "add_table",
    "remove_table",
    "add_column",
    "remove_column",
    "modify_type",
}


# --- createsuperuser -------------------------------------------------------

def _register_createsuperuser(parser):
    parser.add_argument("--username", help="Login name (will prompt if omitted)")


async def _run_createsuperuser(args, conn):
    auth = SqlAlchemyAuthRepository(conn)
    username = args.username
    if not username:
        username = input("Username: ").strip()
    if not username:
        raise ManagementCommandError("username is required")

    if await auth.get_member_by_name(username):
        raise ManagementCommandError(f"a member named '{username}' already exists")

    while True:
        password = getpass.getpass("Password: ")
        if not password:
            print("Password cannot be empty.")
            continue
        confirm = getpass.getpass("Password (again): ")
        if password != confirm:
            print("Error: passwords do not match.")
            continue
        break

    member_id = f"user_{uuid.uuid4().hex[:8]}"
    await auth.create_member(
        member_id, "human", username, password, "superadmin", avatar="🧑"
    )
    print(f"Superuser '{username}' created (id={member_id}).")


# --- changepassword --------------------------------------------------------

def _register_changepassword(parser):
    parser.add_argument("username", help="Name of the member to reset")
    parser.add_argument("--password", help="New password (non-interactive)")
    parser.add_argument(
        "--no-input",
        action="store_true",
        help="Use --password without prompting (requires --password)",
    )


async def _run_changepassword(args, conn):
    auth = SqlAlchemyAuthRepository(conn)
    member = await auth.get_member_by_name(args.username)
    if member is None:
        raise ManagementCommandError(f"no member named '{args.username}'")

    if args.no_input:
        if not args.password:
            raise ManagementCommandError("--password is required with --no-input")
        password = args.password
    else:
        while True:
            password = args.password or getpass.getpass("New password: ")
            if not password:
                print("Password cannot be empty.")
                args.password = None
                continue
            confirm = getpass.getpass("New password (again): ")
            if password != confirm:
                print("Error: passwords do not match.")
                args.password = None
                continue
            break

    # Reuse the same INSERT/UPDATE shape the app uses elsewhere by going through
    # the auth repository's storage. We set the hash directly via a parameterized
    # UPDATE so any role (incl. superadmin) keeps its identity while the password
    # rotates.
    await conn.execute(
        "UPDATE member SET password_hash = ? WHERE id = ?",
        (hash_password(password), member["id"]),
    )
    print(f"Password updated for '{args.username}' (id={member['id']}).")


# --- makemigrations --------------------------------------------------------

def _register_makemigrations(parser):
    parser.add_argument(
        "--name",
        default="auto",
        help="Revision message slug (default: auto)",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="Exit non-zero if the models and database are out of sync "
        "(do not write a migration file)",
    )


def _run_makemigrations(args) -> None:
    """Detect model<->DB drift via Alembic autogenerate (Django-style).

    ``--check`` (CI mode) exits non-zero when the models and the connected
    database differ, so a forgotten schema change can't be merged silently.
    Without ``--check`` it writes a new revision file (or reports that the
    models are already in sync). This is a synchronous command: the comparison
    uses a synchronous engine, and generation delegates to Alembic's own
    ``command.revision`` (which runs env.py's migration loop).
    """
    from alembic import command
    from alembic.autogenerate import compare_metadata
    from alembic.migration import MigrationContext
    from sqlalchemy import create_engine as sync_create_engine

    settings = Settings()
    root = Path(__file__).resolve().parents[3]
    config = Config(str(root / "alembic.ini"))
    config.set_main_option("script_location", str(root / "migrations"))
    config.set_main_option("sqlalchemy.url", settings.database_url.replace("%", "%%"))
    # The application owns logging; keep Alembic's verbosity down here.
    config.attributes["configure_logger"] = False
    logging.getLogger("alembic").setLevel(logging.WARNING)

    script = ScriptDirectory.from_config(config)

    def _compare(connection):
        # ``connection`` is a synchronous DBAPI connection.
        # SQLite can't reliably introspect unbounded String (== TEXT), NOT NULL,
        # check constraints, or FK ondelete, so comparing them only yields
        # false-positive drift. PostgreSQL keeps the strict checks.
        dialect_name = connection.dialect.name
        migration_ctx = MigrationContext.configure(
            connection,
            opts={
                "compare_type": dialect_name != "sqlite",
                "compare_nullable": dialect_name != "sqlite",
                "compare_constraints": dialect_name != "sqlite",
                "compare_server_default": False,
            },
        )
        raw_diff = compare_metadata(migration_ctx, Base.metadata)
        if dialect_name == "sqlite":
            def _op_name(d):
                head = d[0]
                return head if isinstance(head, str) else getattr(head, "op", None)

            # Keep only structural drift SQLite can represent reliably.
            raw_diff = [d for d in raw_diff if _op_name(d) in _SQLITE_MEANINGFUL]
        return raw_diff

    # A synchronous engine lets us compare without an event loop (and without the
    # async driver's run_sync return-value quirk). Map async URLs to their sync
    # dialect (aiosqlite->sqlite, asyncpg->postgresql).
    sync_url = settings.database_url
    assert sync_url is not None
    for async_driver, sync_driver in (
        ("sqlite+aiosqlite", "sqlite"),
        ("postgresql+asyncpg", "postgresql"),
    ):
        if sync_url.startswith(async_driver):
            sync_url = sync_driver + sync_url[len(async_driver):]
            break

    has_changes = False
    with sync_create_engine(sync_url).connect() as sync_conn:
        diff = _compare(sync_conn)
        has_changes = bool(diff)

    if args.check:
        if has_changes:
            print(
                "error: models are out of sync with the database. "
                "Run 'crewspace-manage makemigrations' to generate a migration.",
                file=sys.stderr,
            )
            raise ManagementCommandError("database schema drift detected")
        head_rev = script.get_current_head()
        print(f"No changes detected against database (head={head_rev}). Models are in sync.")
        return

    if not has_changes:
        print("No changes detected against database; nothing to migrate.")
        return

    command.revision(
        config,
        message=args.name,
        autogenerate=True,
        rev_id=_next_rev_id(script),
    )
    print(f"Generated migration for {len(diff)} detected change(s).")


def _next_rev_id(script: ScriptDirectory) -> str:
    """Derive a YYYYMMDD_NN-style revision id, avoiding collisions with existing."""
    today = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%d")
    prefix = f"{today}_"
    existing = [r.revision for r in script.walk_revisions()]
    n = 1
    while f"{prefix}{n:02d}" in existing:
        n += 1
    return f"{prefix}{n:02d}"


COMMANDS = {
    "createsuperuser": (_register_createsuperuser, _run_createsuperuser),
    "changepassword": (_register_changepassword, _run_changepassword),
}
# Synchronous commands are run without opening a DB connection via run_async.
SYNC_COMMANDS = {
    "makemigrations": (_register_makemigrations, _run_makemigrations),
    "backup": (register_backup, run_backup),
    "restore": (register_restore, run_restore),
}
