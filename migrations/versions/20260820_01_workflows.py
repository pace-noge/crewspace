"""Add executable workflows and run logs."""
from __future__ import annotations

from alembic import op
from sqlalchemy import Column, String
from sqlalchemy import inspect as sa_inspect
from crewspace.infrastructure.models import WorkflowModel, WorkflowRunModel

revision = "20260820_01"
down_revision = "20260817_01"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    WorkflowModel.__table__.create(bind=bind, checkfirst=True)
    WorkflowRunModel.__table__.create(bind=bind, checkfirst=True)
    columns = {item["name"] for item in sa_inspect(bind).get_columns("workflow")}
    if "next_run_at" not in columns:
        op.add_column("workflow", Column("next_run_at", String, nullable=True))


def downgrade() -> None:
    bind = op.get_bind()
    WorkflowRunModel.__table__.drop(bind=bind, checkfirst=True)
    WorkflowModel.__table__.drop(bind=bind, checkfirst=True)
