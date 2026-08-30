"""Add recoverable archive state to boards and board columns.

Idempotent for fresh databases, whose initial revision builds the current
SQLAlchemy metadata (and therefore already has these columns).
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect as sa_inspect


revision = "20260826_02"
down_revision = "20260826_01"
branch_labels = None
depends_on = None


def _columns(table: str) -> set[str]:
    return {column["name"] for column in sa_inspect(op.get_bind()).get_columns(table)}


def upgrade() -> None:
    tables = set(sa_inspect(op.get_bind()).get_table_names())
    if "board" in tables and "archived_at" not in _columns("board"):
        with op.batch_alter_table("board") as batch:
            batch.add_column(sa.Column("archived_at", sa.String(), nullable=True))
    if "board_column" in tables and "archived_at" not in _columns("board_column"):
        with op.batch_alter_table("board_column") as batch:
            batch.add_column(sa.Column("archived_at", sa.String(), nullable=True))


def downgrade() -> None:
    tables = set(sa_inspect(op.get_bind()).get_table_names())
    if "board_column" in tables and "archived_at" in _columns("board_column"):
        with op.batch_alter_table("board_column") as batch:
            batch.drop_column("archived_at")
    if "board" in tables and "archived_at" in _columns("board"):
        with op.batch_alter_table("board") as batch:
            batch.drop_column("archived_at")
