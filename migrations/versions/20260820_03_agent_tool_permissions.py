"""Add per-agent tool permissions.

Revision ID: 20260820_03
Revises: 20260820_02
"""
from __future__ import annotations

import datetime as dt

from alembic import op
from sqlalchemy import (
    CheckConstraint,
    Column,
    ForeignKey,
    Integer,
    String,
    inspect as sa_inspect,
    text,
)

revision = "20260820_03"
down_revision = "20260820_02"
branch_labels = None
depends_on = None

_COMPATIBILITY_TOOLS = (
    "create_card",
    "move_card",
    "comment_card",
    "find_card",
    "list_columns",
    "list_boards",
    "post_message",
    "create_cronjob",
)


def upgrade() -> None:
    bind = op.get_bind()
    if "agent_tool_permission" not in sa_inspect(bind).get_table_names():
        op.create_table(
            "agent_tool_permission",
            Column(
                "agent_id",
                String,
                ForeignKey("member.id", ondelete="CASCADE"),
                primary_key=True,
            ),
            Column("provider_type", String, primary_key=True),
            Column("provider_id", String, primary_key=True),
            Column("tool_name", String, primary_key=True),
            Column("enabled", Integer, nullable=False, server_default="1"),
            Column(
                "approval_mode", String, nullable=False, server_default="automatic"
            ),
            Column("created_at", String, nullable=False),
            Column("updated_at", String, nullable=False),
            CheckConstraint(
                "provider_type IN ('native','mcp')",
                name="ck_agent_tool_provider_type",
            ),
            CheckConstraint(
                "approval_mode IN ('automatic','require_approval')",
                name="ck_agent_tool_approval_mode",
            ),
        )
    now = dt.datetime.now(dt.timezone.utc).isoformat()
    builtin_agent_ids = list(bind.execute(
        text("SELECT id FROM member WHERE kind='agent' AND pubkey IS NULL")
    ).scalars())
    for agent_id in builtin_agent_ids:
        for tool_name in _COMPATIBILITY_TOOLS:
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
    if "agent_tool_permission" in sa_inspect(op.get_bind()).get_table_names():
        op.drop_table("agent_tool_permission")
