"""Add per-member channel read state for unread indicators.

Idempotent for fresh databases: the initial revision builds current declarative
metadata, so ``channel_read_state`` may already exist before this revision runs.
Legacy databases at 20260831_01 receive it once.
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect as sa_inspect

revision = "20260901_01"
down_revision = "20260831_01"


def upgrade() -> None:
    bind = op.get_bind()
    if "channel_read_state" not in sa_inspect(bind).get_table_names():
        op.create_table(
            "channel_read_state",
            sa.Column(
                "channel_id", sa.String(),
                sa.ForeignKey("channel.id", ondelete="CASCADE"), primary_key=True,
            ),
            sa.Column(
                "member_id", sa.String(),
                sa.ForeignKey("member.id", ondelete="CASCADE"), primary_key=True,
            ),
            sa.Column("last_read_at", sa.String(), nullable=False),
        )


def downgrade() -> None:
    bind = op.get_bind()
    if "channel_read_state" in sa_inspect(bind).get_table_names():
        op.drop_table("channel_read_state")