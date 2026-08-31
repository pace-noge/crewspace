"""Atomic SQLite restore management command."""
from __future__ import annotations

import argparse
import stat
from pathlib import Path

from . import ManagementCommandError
from .backup import (
    atomic_sqlite_copy,
    configured_sqlite_path,
    paths_refer_to_same_file,
    sqlite_integrity_ok,
)


def register_restore(parser: argparse.ArgumentParser) -> None:
    """Restore a database snapshot while Crewspace is stopped."""
    parser.add_argument("snapshot", type=Path, help="SQLite snapshot to restore")


def run_restore(args: argparse.Namespace) -> None:
    destination = configured_sqlite_path(operation="restore")
    source = args.snapshot.expanduser().resolve()
    if not source.is_file():
        raise ManagementCommandError(f"snapshot does not exist: {source}")
    if paths_refer_to_same_file(source, destination):
        raise ManagementCommandError("source and destination resolve to the same file")
    if not sqlite_integrity_ok(source):
        raise ManagementCommandError(f"snapshot is not a valid SQLite database: {source}")

    existing_mode = stat.S_IMODE(destination.stat().st_mode) if destination.exists() else None
    atomic_sqlite_copy(
        source,
        destination,
        operation="restore",
        destination_mode=existing_mode,
    )
    try:
        for suffix in ("-wal", "-shm"):
            Path(f"{destination}{suffix}").unlink(missing_ok=True)
    except OSError as exc:
        raise ManagementCommandError(f"restore sidecar cleanup failed: {exc}") from exc
    print(f"Database restored from {source} to {destination}")
