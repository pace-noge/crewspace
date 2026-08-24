"""Add bounded failure reason to coding_run."""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect as sa_inspect

revision = "20260825_01"
down_revision = "20260824_04"
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
    if "failure_reason" not in existing:
        with op.batch_alter_table("coding_run") as batch:
            batch.add_column(
                sa.Column("failure_reason", sa.Text(), nullable=False, server_default="")
            )
    op.execute("UPDATE coding_run SET failure_reason='' WHERE failure_reason IS NULL")


def downgrade() -> None:
    if not _has_coding_runs():
        return
    existing = _columns()
    if "failure_reason" in existing:
        with op.batch_alter_table("coding_run") as batch:
            batch.drop_column("failure_reason")
