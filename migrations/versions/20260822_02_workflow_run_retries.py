"""Add workflow run retry lineage and attempt metadata."""
from __future__ import annotations

from alembic import op
from sqlalchemy import Column, Integer, String
from sqlalchemy import inspect as sa_inspect

revision = "20260822_02"
down_revision = "20260822_01"
branch_labels = None
depends_on = None


_COLUMNS = (
    Column("parent_run_id", String, nullable=True),
    Column("root_run_id", String, nullable=True),
    Column("attempt", Integer, nullable=False, server_default="1"),
    Column("retry_initiated_by", String, nullable=True),
)


def upgrade() -> None:
    bind = op.get_bind()
    existing = {item["name"] for item in sa_inspect(bind).get_columns("workflow_run")}
    with op.batch_alter_table("workflow_run") as batch:
        for column in _COLUMNS:
            if column.name not in existing:
                batch.add_column(column)


def downgrade() -> None:
    bind = op.get_bind()
    existing = {item["name"] for item in sa_inspect(bind).get_columns("workflow_run")}
    with op.batch_alter_table("workflow_run") as batch:
        for name in reversed([column.name for column in _COLUMNS]):
            if name in existing:
                batch.drop_column(name)
