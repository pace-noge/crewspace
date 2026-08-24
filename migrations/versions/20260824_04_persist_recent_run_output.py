"""Persist bounded recent output on coding_run."""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect as sa_inspect

revision = "20260824_04"
down_revision = "20260824_03"
branch_labels = None
depends_on = None


def _has_coding_runs() -> bool:
    return "coding_run" in sa_inspect(op.get_bind()).get_table_names()


def _columns() -> set[str]:
    return {
        column["name"]
        for column in sa_inspect(op.get_bind()).get_columns("coding_run")
    }


def upgrade() -> None:
    if not _has_coding_runs():
        return
    existing = _columns()
    if "recent_output" not in existing:
        with op.batch_alter_table("coding_run") as batch:
            batch.add_column(
                sa.Column("recent_output", sa.Text(), nullable=False, server_default="")
            )
    op.execute("UPDATE coding_run SET recent_output='' WHERE recent_output IS NULL")


def downgrade() -> None:
    if not _has_coding_runs():
        return
    existing = _columns()
    if "recent_output" in existing:
        with op.batch_alter_table("coding_run") as batch:
            batch.drop_column("recent_output")
