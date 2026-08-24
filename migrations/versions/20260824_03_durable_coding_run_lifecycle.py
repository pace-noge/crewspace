"""Add durable coding-run lifecycle states and timestamps."""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect as sa_inspect

revision = "20260824_03"
down_revision = "20260824_02"
branch_labels = None
depends_on = None

_OLD_STATUS_CHECK = "status IN ('running','captured','failed')"
_NEW_STATUS_CHECK = (
    "status IN ('queued','running','succeeded','failed','cancelled',"
    "'timed_out','interrupted')"
)
_TRANSITION_STATUS_CHECK = (
    "status IN ('queued','running','captured','succeeded','failed','cancelled',"
    "'timed_out','interrupted')"
)


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
    with op.batch_alter_table("coding_run") as batch:
        if "updated_at" not in existing:
            batch.add_column(sa.Column("updated_at", sa.String(), nullable=True))
        if "started_at" not in existing:
            batch.add_column(sa.Column("started_at", sa.String(), nullable=True))
        if "finished_at" not in existing:
            batch.add_column(sa.Column("finished_at", sa.String(), nullable=True))
        batch.drop_constraint("ck_coding_run_status", type_="check")
        batch.create_check_constraint("ck_coding_run_status", _TRANSITION_STATUS_CHECK)
    op.execute("UPDATE coding_run SET status='succeeded' WHERE status='captured'")
    op.execute("UPDATE coding_run SET updated_at=created_at WHERE updated_at IS NULL")
    op.execute(
        "UPDATE coding_run SET started_at=created_at "
        "WHERE status='running' AND started_at IS NULL"
    )
    op.execute(
        "UPDATE coding_run SET finished_at=updated_at "
        "WHERE status IN ('succeeded','failed','cancelled','timed_out') "
        "AND finished_at IS NULL"
    )
    with op.batch_alter_table("coding_run") as batch:
        batch.drop_constraint("ck_coding_run_status", type_="check")
        batch.create_check_constraint("ck_coding_run_status", _NEW_STATUS_CHECK)
        batch.alter_column("updated_at", existing_type=sa.String(), nullable=False)


def downgrade() -> None:
    if not _has_coding_runs():
        return
    with op.batch_alter_table("coding_run") as batch:
        batch.drop_constraint("ck_coding_run_status", type_="check")
        batch.create_check_constraint("ck_coding_run_status", _TRANSITION_STATUS_CHECK)
    op.execute("UPDATE coding_run SET status='captured' WHERE status='succeeded'")
    op.execute(
        "UPDATE coding_run SET status='failed' "
        "WHERE status IN ('cancelled','timed_out')"
    )
    op.execute(
        "UPDATE coding_run SET status='running' "
        "WHERE status IN ('queued','interrupted')"
    )
    existing = _columns()
    with op.batch_alter_table("coding_run") as batch:
        batch.drop_constraint("ck_coding_run_status", type_="check")
        batch.create_check_constraint("ck_coding_run_status", _OLD_STATUS_CHECK)
        for column in ("finished_at", "started_at", "updated_at"):
            if column in existing:
                batch.drop_column(column)
