"""M9.5 — atomic SQLite backup/restore management commands."""
from __future__ import annotations

import argparse
import datetime as dt
import os
import sqlite3
import stat
import subprocess
import sys
from pathlib import Path

from crewspace.management import backup as backup_module


def _database_url(path: Path) -> str:
    return f"sqlite+aiosqlite:///{path}"


def _env(path: Path) -> dict[str, str]:
    return {**os.environ, "CREWSPACE_DATABASE_URL": _database_url(path)}


def _run(*args: str, env: dict[str, str], cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "crewspace.management.cli", *args],
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )


def _seed(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE IF NOT EXISTS backup_probe(value TEXT NOT NULL)")
        conn.execute("DELETE FROM backup_probe")
        conn.execute("INSERT INTO backup_probe(value) VALUES (?)", (value,))


def _value(path: Path) -> str:
    with sqlite3.connect(path) as conn:
        row = conn.execute("SELECT value FROM backup_probe").fetchone()
    assert row is not None
    return row[0]


def test_backup_and_restore_round_trip_preserves_snapshot(tmp_path: Path) -> None:
    database = tmp_path / "live.db"
    snapshot = tmp_path / "snapshots" / "crewspace.db"
    _seed(database, "before")

    backup = _run("backup", "--out", str(snapshot), env=_env(database))
    assert backup.returncode == 0, backup.stdout + backup.stderr
    assert snapshot.is_file()
    assert _value(snapshot) == "before"

    _seed(database, "after")
    restore = _run("restore", str(snapshot), env=_env(database))
    assert restore.returncode == 0, restore.stdout + restore.stderr
    assert _value(database) == "before"


def test_backup_without_out_uses_timestamped_file_in_backups_directory(tmp_path: Path) -> None:
    database = tmp_path / "live.db"
    _seed(database, "default-path")

    result = _run("backup", env=_env(database), cwd=tmp_path)

    assert result.returncode == 0, result.stdout + result.stderr
    snapshots = list((tmp_path / "backups").glob("crewspace-*.db"))
    assert len(snapshots) == 1
    assert _value(snapshots[0]) == "default-path"


def test_backup_rejects_missing_source_without_creating_it(tmp_path: Path) -> None:
    database = tmp_path / "missing.db"
    snapshot = tmp_path / "snapshot.db"

    result = _run("backup", "--out", str(snapshot), env=_env(database))

    assert result.returncode == 1
    assert "does not exist" in result.stderr
    assert "Traceback" not in result.stderr
    assert not database.exists()
    assert not snapshot.exists()


def test_restore_rejects_missing_snapshot_and_preserves_live_database(tmp_path: Path) -> None:
    database = tmp_path / "live.db"
    _seed(database, "preserved")

    result = _run("restore", str(tmp_path / "missing.db"), env=_env(database))

    assert result.returncode == 1
    assert "does not exist" in result.stderr
    assert "Traceback" not in result.stderr
    assert _value(database) == "preserved"


def test_restore_rejects_corrupt_snapshot_and_preserves_live_database(tmp_path: Path) -> None:
    database = tmp_path / "live.db"
    snapshot = tmp_path / "corrupt.db"
    _seed(database, "preserved")
    snapshot.write_bytes(b"not a sqlite database")

    result = _run("restore", str(snapshot), env=_env(database))

    assert result.returncode == 1
    assert "valid SQLite" in result.stderr
    assert "Traceback" not in result.stderr
    assert _value(database) == "preserved"


def test_backup_and_restore_reject_source_target_alias(tmp_path: Path) -> None:
    database = tmp_path / "live.db"
    _seed(database, "preserved")

    backup = _run("backup", "--out", str(database), env=_env(database))
    restore = _run("restore", str(database), env=_env(database))

    assert backup.returncode == 1
    assert restore.returncode == 1
    assert "same file" in (backup.stderr + restore.stderr)
    assert _value(database) == "preserved"


def test_backup_and_restore_reject_hard_link_aliases(tmp_path: Path) -> None:
    database = tmp_path / "live.db"
    alias = tmp_path / "same-inode.db"
    _seed(database, "preserved")
    os.link(database, alias)

    backup = _run("backup", "--out", str(alias), env=_env(database))
    restore = _run("restore", str(alias), env=_env(database))

    assert backup.returncode == 1
    assert restore.returncode == 1
    assert "same file" in backup.stderr + restore.stderr
    assert _value(database) == "preserved"


def test_restore_preserves_all_target_permission_bits(tmp_path: Path) -> None:
    database = tmp_path / "live.db"
    snapshot = tmp_path / "snapshot.db"
    _seed(database, "snapshot")
    assert _run("backup", "--out", str(snapshot), env=_env(database)).returncode == 0
    _seed(database, "changed")
    os.chmod(database, 0o6750)

    restore = _run("restore", str(snapshot), env=_env(database))

    assert restore.returncode == 0, restore.stdout + restore.stderr
    assert stat.S_IMODE(database.stat().st_mode) == 0o6750


def test_default_backup_paths_are_unique_when_timestamps_collide(
    tmp_path: Path, monkeypatch
) -> None:
    database = tmp_path / "live.db"
    _seed(database, "preserved")
    monkeypatch.setenv("CREWSPACE_DATABASE_URL", _database_url(database))
    monkeypatch.chdir(tmp_path)

    class FrozenDateTime(dt.datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(2026, 8, 31, 12, 0, tzinfo=tz)

    monkeypatch.setattr(backup_module.dt, "datetime", FrozenDateTime)
    backup_module.run_backup(argparse.Namespace(out=None))
    backup_module.run_backup(argparse.Namespace(out=None))

    snapshots = list((tmp_path / "backups").glob("crewspace-*.db"))
    assert len(snapshots) == 2
    assert all(_value(path) == "preserved" for path in snapshots)


def test_restore_removes_stale_wal_sidecars(tmp_path: Path) -> None:
    database = tmp_path / "live.db"
    snapshot = tmp_path / "snapshot.db"
    _seed(database, "snapshot")
    backup = _run("backup", "--out", str(snapshot), env=_env(database))
    assert backup.returncode == 0

    Path(f"{database}-wal").write_bytes(b"stale")
    Path(f"{database}-shm").write_bytes(b"stale")
    restore = _run("restore", str(snapshot), env=_env(database))

    assert restore.returncode == 0, restore.stdout + restore.stderr
    assert not Path(f"{database}-wal").exists()
    assert not Path(f"{database}-shm").exists()


def test_backup_filesystem_failure_is_clean(tmp_path: Path) -> None:
    database = tmp_path / "live.db"
    _seed(database, "preserved")
    impossible = tmp_path / "not-a-directory"
    impossible.write_text("file")

    result = _run("backup", "--out", str(impossible / "snapshot.db"), env=_env(database))

    assert result.returncode == 1
    assert "backup failed" in result.stderr
    assert "Traceback" not in result.stderr
    assert _value(database) == "preserved"


def test_restore_filesystem_failure_is_clean_and_preserves_target(tmp_path: Path) -> None:
    database = tmp_path / "not-a-database"
    snapshot = tmp_path / "snapshot.db"
    database.mkdir()
    _seed(snapshot, "snapshot")

    result = _run("restore", str(snapshot), env=_env(database))

    assert result.returncode == 1
    assert "restore failed" in result.stderr
    assert "Traceback" not in result.stderr
    assert database.is_dir()


def test_in_memory_sqlite_is_rejected_cleanly(tmp_path: Path) -> None:
    env = {**os.environ, "CREWSPACE_DATABASE_URL": "sqlite+aiosqlite:///:memory:"}

    backup = _run("backup", "--out", str(tmp_path / "snapshot.db"), env=env)
    restore = _run("restore", str(tmp_path / "snapshot.db"), env=env)

    assert backup.returncode == 1
    assert restore.returncode == 1
    assert "file-backed SQLite" in backup.stderr + restore.stderr
    assert "Traceback" not in backup.stderr + restore.stderr


def test_postgres_backend_fails_cleanly_with_operator_guidance(tmp_path: Path) -> None:
    env = {
        **os.environ,
        "CREWSPACE_DATABASE_URL": "postgresql+asyncpg://user:pass@db/crewspace",
    }

    backup = _run("backup", "--out", str(tmp_path / "snapshot.dump"), env=env)
    restore = _run("restore", str(tmp_path / "snapshot.dump"), env=env)

    assert backup.returncode == 1
    assert restore.returncode == 1
    assert "pg_dump" in backup.stderr
    assert "psql" in restore.stderr
    assert "Traceback" not in backup.stderr + restore.stderr
