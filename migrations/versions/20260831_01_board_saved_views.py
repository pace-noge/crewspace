"""Add persisted, owner-scoped board saved views.

Idempotent for fresh databases: the initial revision builds current declarative
metadata, so ``board_saved_view`` may already exist before this revision runs.
Legacy databases at 20260830_02 receive it once.
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect as sa_inspect

revision = "20260831_01"
down_revision = "20260830_02"


def upgrade() -> None:
    bind = op.get_bind()
    if "board_saved_view" not in sa_inspect(bind).get_table_names():
        op.create_table(
            "board_saved_view",
            sa.Column("id", sa.String(), primary_key=True),
            sa.Column(
                "board_id", sa.String(),
                sa.ForeignKey("board.id", ondelete="CASCADE"), nullable=False,
            ),
            sa.Column(
                "owner_id", sa.String(),
                sa.ForeignKey("member.id", ondelete="CASCADE"), nullable=False,
            ),
            sa.Column("name", sa.String(), nullable=False),
            sa.Column(
                "view", sa.String(), nullable=False, server_default="board"
            ),
            sa.Column("filters", sa.Text(), nullable=True),  # JSON string
            sa.Column("grouping", sa.Text(), nullable=True),  # JSON string
            sa.Column("created_at", sa.String(), nullable=False),
        )


def downgrade() -> None:
    bind = op.get_bind()
    if "board_saved_view" in sa_inspect(bind).get_table_names():
        op.drop_table("board_saved_view")
