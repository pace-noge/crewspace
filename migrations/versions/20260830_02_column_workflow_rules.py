"""Add board column → workflow trigger rules + idempotent trigger log.

Idempotent for fresh databases: the initial revision builds current declarative
metadata, so ``column_workflow_rule`` and ``column_move_trigger`` may already
exist before this revision runs. Legacy databases at 20260830_01 receive them
once.
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect as sa_inspect

revision = "20260830_02"
down_revision = "20260830_01"


def _table_exists(bind, table: str) -> bool:
    return table in sa_inspect(bind).get_table_names()


def upgrade() -> None:
    bind = op.get_bind()
    if not _table_exists(bind, "column_workflow_rule"):
        op.create_table(
            "column_workflow_rule",
            sa.Column("id", sa.String(), primary_key=True),
            sa.Column("board_id", sa.String(), sa.ForeignKey("board.id"), nullable=False),
            sa.Column(
                "column_id", sa.String(),
                sa.ForeignKey("board_column.id", ondelete="CASCADE"), nullable=False,
            ),
            sa.Column(
                "workflow_id", sa.String(),
                sa.ForeignKey("workflow.id", ondelete="CASCADE"), nullable=False,
            ),
            sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
            sa.Column("changed_by", sa.String(), sa.ForeignKey("member.id"), nullable=False),
            sa.UniqueConstraint("column_id", name="uq_column_workflow_rule_column"),
        )
    if not _table_exists(bind, "column_move_trigger"):
        op.create_table(
            "column_move_trigger",
            sa.Column("id", sa.String(), primary_key=True),
            sa.Column("card_id", sa.String(), sa.ForeignKey("card.id", ondelete="CASCADE"), nullable=False),
            sa.Column(
                "column_id", sa.String(),
                sa.ForeignKey("board_column.id", ondelete="CASCADE"), nullable=False,
            ),
            sa.Column(
                "workflow_id", sa.String(),
                sa.ForeignKey("workflow.id", ondelete="CASCADE"), nullable=False,
            ),
            sa.Column("board_id", sa.String(), sa.ForeignKey("board.id"), nullable=False),
            sa.Column("event_key", sa.String(), nullable=False),
            sa.Column(
                "run_id", sa.String(),
                sa.ForeignKey("workflow_run.id", ondelete="SET NULL"), nullable=True,
            ),
            sa.Column("created_at", sa.String(), nullable=False),
            sa.UniqueConstraint(
                "card_id", "column_id", "workflow_id", "event_key",
                name="uq_column_move_trigger",
            ),
        )
    # Widen the workflow trigger_type CHECK to accept 'column_move' (SQLite
    # cannot reflect constraints, so compare_constraints=False hides it; we must
    # rebuild it explicitly for both fresh and legacy DBs).
    bind = op.get_bind()
    with op.batch_alter_table("workflow") as batch:
        batch.drop_constraint("ck_workflow_trigger_type", type_="check")
        batch.create_check_constraint(
            "ck_workflow_trigger_type",
            "trigger_type IN ('message_posted','reaction_added','diff_posted','webhook','schedule','column_move')",
        )


def downgrade() -> None:
    bind = op.get_bind()
    # Legacy code cannot dispatch column_move. Disable and remap those rows to a
    # legacy-valid trigger before narrowing the CHECK, otherwise SQLite's batch
    # table rebuild rejects populated databases.
    op.execute(
        "UPDATE workflow SET enabled = 0, trigger_type = 'webhook' "
        "WHERE trigger_type = 'column_move'"
    )
    with op.batch_alter_table("workflow") as batch:
        batch.drop_constraint("ck_workflow_trigger_type", type_="check")
        batch.create_check_constraint(
            "ck_workflow_trigger_type",
            "trigger_type IN ('message_posted','reaction_added','diff_posted','webhook','schedule')",
        )
    if _table_exists(bind, "column_move_trigger"):
        op.drop_table("column_move_trigger")
    if _table_exists(bind, "column_workflow_rule"):
        op.drop_table("column_workflow_rule")
