"""Add workflow schedule execution leases.

Revision ID: 20260822_01
Revises: 20260820_05
"""
from __future__ import annotations

from alembic import op
from sqlalchemy import Column, String
from sqlalchemy import inspect as sa_inspect

revision = "20260822_01"
down_revision = "20260820_05"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    columns = {item["name"] for item in sa_inspect(bind).get_columns("workflow")}
    if "claim_token" not in columns:
        op.add_column("workflow", Column("claim_token", String, nullable=True))
    if "claim_until" not in columns:
        op.add_column("workflow", Column("claim_until", String, nullable=True))


def downgrade() -> None:
    bind = op.get_bind()
    columns = {item["name"] for item in sa_inspect(bind).get_columns("workflow")}
    with op.batch_alter_table("workflow") as batch_op:
        if "claim_until" in columns:
            batch_op.drop_column("claim_until")
        if "claim_token" in columns:
            batch_op.drop_column("claim_token")