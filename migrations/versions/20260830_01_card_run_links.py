"""Add durable card ↔ coding-run links.

Idempotent for fresh databases: the initial revision builds current declarative
metadata, so ``card_run_link`` may already exist before this revision runs.
Legacy databases at 20260826_02 receive the table once.
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect as sa_inspect


revision = "20260830_01"
down_revision = "20260826_02"
branch_labels = None
depends_on = None


def upgrade() -> None:
    tables = set(sa_inspect(op.get_bind()).get_table_names())
    if "card_run_link" in tables:
        return
    op.create_table(
        "card_run_link",
        sa.Column(
            "card_id",
            sa.String(),
            sa.ForeignKey("card.id", ondelete="CASCADE"),
            primary_key=True,
            nullable=False,
        ),
        sa.Column(
            "run_id",
            sa.String(),
            sa.ForeignKey("coding_run.id", ondelete="CASCADE"),
            primary_key=True,
            nullable=False,
        ),
        sa.Column(
            "linked_by",
            sa.String(),
            sa.ForeignKey("member.id"),
            nullable=False,
        ),
        sa.Column("linked_at", sa.String(), nullable=False),
        sa.UniqueConstraint("card_id", "run_id", name="uq_card_run_link"),
    )


def downgrade() -> None:
    if "card_run_link" in set(sa_inspect(op.get_bind()).get_table_names()):
        op.drop_table("card_run_link")
