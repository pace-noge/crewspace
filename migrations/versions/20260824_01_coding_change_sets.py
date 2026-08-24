"""Add team-scoped coding runs, change sets, and audit events."""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect as sa_inspect

revision = "20260824_01"
down_revision = "20260822_02"
branch_labels = None
depends_on = None


def _tables() -> set[str]:
    return set(sa_inspect(op.get_bind()).get_table_names())


def upgrade() -> None:
    existing = _tables()
    if "coding_repository" not in existing:
        op.create_table(
            "coding_repository",
            sa.Column("id", sa.String(), primary_key=True),
            sa.Column("name", sa.String(), nullable=False),
            sa.Column("default_branch", sa.String(), nullable=False),
            sa.Column("created_by", sa.String(), sa.ForeignKey("member.id"), nullable=False),
            sa.Column("created_at", sa.String(), nullable=False),
        )
    if "team_coding_repository" not in existing:
        op.create_table(
            "team_coding_repository",
            sa.Column("team_id", sa.String(), sa.ForeignKey("team.id", ondelete="CASCADE"), primary_key=True),
            sa.Column("repository_id", sa.String(), sa.ForeignKey("coding_repository.id", ondelete="CASCADE"), primary_key=True),
            sa.Column("granted_by", sa.String(), sa.ForeignKey("member.id"), nullable=False),
            sa.Column("granted_at", sa.String(), nullable=False),
        )
    if "coding_run" not in existing:
        op.create_table(
            "coding_run",
            sa.Column("id", sa.String(), primary_key=True),
            sa.Column("team_id", sa.String(), sa.ForeignKey("team.id"), nullable=False),
            sa.Column("repository_id", sa.String(), sa.ForeignKey("coding_repository.id"), nullable=False),
            sa.Column("requested_by", sa.String(), sa.ForeignKey("member.id"), nullable=False),
            sa.Column("agent_id", sa.String(), sa.ForeignKey("member.id"), nullable=False),
            sa.Column("request_id", sa.String(), nullable=False),
            sa.Column("instruction", sa.Text(), nullable=False),
            sa.Column("status", sa.String(), nullable=False),
            sa.Column("created_at", sa.String(), nullable=False),
            sa.CheckConstraint("status IN ('running','captured','failed')", name="ck_coding_run_status"),
            sa.UniqueConstraint("agent_id", "request_id", name="uq_coding_run_agent_request"),
        )
    if "stored_change_set" not in existing:
        op.create_table(
            "stored_change_set",
            sa.Column("id", sa.String(), primary_key=True),
            sa.Column("team_id", sa.String(), sa.ForeignKey("team.id"), nullable=False),
            sa.Column("repository_id", sa.String(), sa.ForeignKey("coding_repository.id"), nullable=False),
            sa.Column("run_id", sa.String(), sa.ForeignKey("coding_run.id"), nullable=False),
            sa.Column("agent_id", sa.String(), sa.ForeignKey("member.id"), nullable=False),
            sa.Column("request_id", sa.String(), nullable=False),
            sa.Column("status", sa.String(), nullable=False),
            sa.Column("payload", sa.Text(), nullable=False),
            sa.Column("created_at", sa.String(), nullable=False),
            sa.CheckConstraint("status IN ('captured','reviewed','pr_requested','retained','discard_requested')", name="ck_stored_change_set_status"),
            sa.UniqueConstraint("run_id", name="uq_stored_change_set_run"),
        )
    if "change_set_audit_event" not in existing:
        op.create_table(
            "change_set_audit_event",
            sa.Column("id", sa.String(), primary_key=True),
            sa.Column("change_set_id", sa.String(), sa.ForeignKey("stored_change_set.id", ondelete="CASCADE"), nullable=False),
            sa.Column("action", sa.String(), nullable=False),
            sa.Column("actor_id", sa.String(), sa.ForeignKey("member.id"), nullable=False),
            sa.Column("created_at", sa.String(), nullable=False),
        )
        op.create_index(
            "ix_change_set_audit_created",
            "change_set_audit_event",
            ["change_set_id", "created_at"],
        )


def downgrade() -> None:
    existing = _tables()
    for table in (
        "change_set_audit_event",
        "stored_change_set",
        "coding_run",
        "team_coding_repository",
        "coding_repository",
    ):
        if table in existing:
            op.drop_table(table)
