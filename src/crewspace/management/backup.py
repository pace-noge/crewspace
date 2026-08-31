"""Offline-safe SQLite backup management command."""
from __future__ import annotations

import argparse
import datetime as dt
import os
import sqlite3
import tempfile
from pathlib import Path

from sqlalchemy.engine import make_url

from ..config import Settings
from . import ManagementCommandError


def register_backup(parser: argparse.ArgumentParser) -> None:
    """Create a consistent database snapshot."""
    parser.add_argument("--out", type=Path, help="Snapshot path (default: backups/crewspace-<timestamp>.db)")


def configured_sqlite_path(*, operation: str) -> Path:
    database_url = Settings().database_url
    assert database_url is not None
    url = make_url(database_url)
    if url.get_backend_name() != "sqlite":
        tool = "pg_dump" if operation == "backup" else "psql"
        raise ManagementCommandError(
            f"PostgreSQL {operation} is not managed by this command; use {tool} with CREWSPACE_DATABASE_URL"
        )
    if not url.database or url.database == ":memory:":
        raise ManagementCommandError(f"{operation} requires a file-backed SQLite database")
    return Path(url.database).expanduser().resolve()


def sqlite_integrity_ok(path: Path) -> bool:
    try:
        with sqlite3.connect(f"file:{path}?mode=ro", uri=True) as conn:
            row = conn.execute("PRAGMA integrity_check").fetchone()
        return row == ("ok",)
    except sqlite3.DatabaseError:
        return False


def _fsync_file(path: Path) -> None:
    with path.open("rb") as handle:
        os.fsync(handle.fileno())


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def paths_refer_to_same_file(source: Path, destination: Path) -> bool:
    if source == destination:
        return True
    try:
        return os.path.samefile(source, destination)
    except (FileNotFoundError, NotADirectoryError):
        return False
    except OSError as exc:
        raise ManagementCommandError(f"source/destination alias check failed: {exc}") from exc


def atomic_sqlite_copy(
    source: Path,
    destination: Path,
    *,
    operation: str,
    destination_mode: int | None = None,
) -> None:
    if paths_refer_to_same_file(source, destination):
        raise ManagementCommandError("source and destination resolve to the same file")

    temporary: Path | None = None
    try:
        destination.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary_name = tempfile.mkstemp(
            prefix=f".{destination.name}.", suffix=".tmp", dir=destination.parent
        )
        os.close(fd)
        temporary = Path(temporary_name)
        with sqlite3.connect(f"file:{source}?mode=ro", uri=True) as src:
            with sqlite3.connect(temporary) as dst:
                src.backup(dst)
        if not sqlite_integrity_ok(temporary):
            raise ManagementCommandError("generated snapshot is not a valid SQLite database")
        if destination_mode is not None:
            os.chmod(temporary, destination_mode)
        _fsync_file(temporary)
        os.replace(temporary, destination)
        _fsync_directory(destination.parent)
    except ManagementCommandError:
        raise
    except (OSError, sqlite3.DatabaseError) as exc:
        raise ManagementCommandError(f"{operation} failed: {exc}") from exc
    finally:
        if temporary is not None:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass


def run_backup(args: argparse.Namespace) -> None:
    source = configured_sqlite_path(operation="backup")
    if not source.is_file():
        raise ManagementCommandError(f"database does not exist: {source}")
    timestamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    reserved_default = args.out is None
    if reserved_default:
        directory = Path("backups").resolve()
        try:
            directory.mkdir(parents=True, exist_ok=True)
            descriptor, name = tempfile.mkstemp(
                prefix=f"crewspace-{timestamp}-", suffix=".db", dir=directory
            )
            os.close(descriptor)
            destination = Path(name)
        except OSError as exc:
            raise ManagementCommandError(f"backup failed: {exc}") from exc
    else:
        destination = args.out.expanduser().resolve()
    try:
        atomic_sqlite_copy(source, destination, operation="backup")
    except ManagementCommandError:
        if reserved_default:
            try:
                destination.unlink(missing_ok=True)
            except OSError:
                pass
        raise
    print(f"Backup written to {destination}")
