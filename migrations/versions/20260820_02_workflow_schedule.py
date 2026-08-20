"""Add workflow schedule due timestamp.

Revision ID: 20260820_02
Revises: 20260820_01
"""
from __future__ import annotations

from alembic import op
from sqlalchemy import Column, String
from sqlalchemy import inspect as sa_inspect

revision = "20260820_02"
down_revision = "20260820_01"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    columns = {item["name"] for item in sa_inspect(bind).get_columns("workflow")}
    if "next_run_at" not in columns:
        op.add_column("workflow", Column("next_run_at", String, nullable=True))


def downgrade() -> None:
    bind = op.get_bind()
    columns = {item["name"] for item in sa_inspect(bind).get_columns("workflow")}
    if "next_run_at" in columns:
        with op.batch_alter_table("workflow") as batch_op:
            batch_op.drop_column("next_run_at")
