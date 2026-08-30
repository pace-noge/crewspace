"""Add card detail metadata (due_date, priority, labels) and card_activity audit.

Keeps the migration idempotent so a fresh DB (whose initial revision already
builds these columns from declarative metadata) is not double-migrated, while an
existing DB at the prior head gains them once. Mirrors the project's
idempotent-column pattern.
"""
from __future__ import annotations

import datetime as dt

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect as sa_inspect, text


revision = "20260826_01"
down_revision = "20260825_01"
branch_labels = None
depends_on = None

# Builtin agents must keep every native board tool as they upgrade. New tools
# added after a legacy agent was seeded need an idempotent backfill here so a
# partial upgrade does not leave a builtin agent behind the current registry.
_NEW_COMPATIBILITY_TOOLS = ("get_card", "update_card")


def _has_card() -> bool:
    return "card" in sa_inspect(op.get_bind()).get_table_names()


def _card_columns() -> set[str]:
    return {column["name"] for column in sa_inspect(op.get_bind()).get_columns("card")}


def _has_card_activity() -> bool:
    return "card_activity" in sa_inspect(op.get_bind()).get_table_names()


def upgrade() -> None:
    bind = op.get_bind()
    if not _has_card():
        return
    existing = _card_columns()
    if {"due_date", "priority", "labels"} - existing:
        with op.batch_alter_table("card") as batch:
            if "due_date" not in existing:
                batch.add_column(sa.Column("due_date", sa.String(), nullable=True))
            if "priority" not in existing:
                batch.add_column(sa.Column("priority", sa.String(), nullable=True))
            if "labels" not in existing:
                batch.add_column(sa.Column("labels", sa.Text(), nullable=True))
    if not _has_card_activity():
        op.create_table(
            "card_activity",
            sa.Column("id", sa.String(), primary_key=True),
            sa.Column("card_id", sa.String(), sa.ForeignKey("card.id", ondelete="CASCADE"), nullable=False),
            sa.Column("actor_id", sa.String(), sa.ForeignKey("member.id"), nullable=True),
            sa.Column("field", sa.String(), nullable=False),
            sa.Column("old_value", sa.Text(), nullable=True),
            sa.Column("new_value", sa.Text(), nullable=True),
            sa.Column("created_at", sa.String(), nullable=False),
        )
    _backfill_builtin_card_tools(bind)


def _backfill_builtin_card_tools(bind) -> None:
    if "agent_tool_permission" not in sa_inspect(bind).get_table_names():
        return
    now = dt.datetime.now(dt.timezone.utc).isoformat()
    builtin_agent_ids = list(bind.execute(
        text("SELECT id FROM member WHERE kind='agent' AND pubkey IS NULL")
    ).scalars())
    for agent_id in builtin_agent_ids:
        for tool_name in _NEW_COMPATIBILITY_TOOLS:
            bind.execute(
                text(
                    "INSERT INTO agent_tool_permission "
                    "(agent_id, provider_type, provider_id, tool_name, enabled, "
                    "approval_mode, created_at, updated_at) "
                    "SELECT :agent_id, 'native', 'crewspace', :tool_name, "
                    "1, 'automatic', :now, :now "
                    "WHERE EXISTS (SELECT 1 FROM member WHERE id=:agent_id) "
                    "AND NOT EXISTS (SELECT 1 FROM agent_tool_permission "
                    "WHERE agent_id=:agent_id AND provider_type='native' "
                    "AND provider_id='crewspace' AND tool_name=:tool_name)"
                ),
                {"agent_id": agent_id, "tool_name": tool_name, "now": now},
            )


def downgrade() -> None:
    if not _has_card():
        return
    existing = _card_columns()
    if {"due_date", "priority", "labels"} & existing:
        with op.batch_alter_table("card") as batch:
            if "labels" in existing:
                batch.drop_column("labels")
            if "priority" in existing:
                batch.drop_column("priority")
            if "due_date" in existing:
                batch.drop_column("due_date")
    if _has_card_activity():
        op.drop_table("card_activity")
