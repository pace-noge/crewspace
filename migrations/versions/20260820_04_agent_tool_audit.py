"""Add durable agent tool-call audit records.

Revision ID: 20260820_04
Revises: 20260820_03
"""
from __future__ import annotations

from alembic import op
from sqlalchemy import CheckConstraint, Column, Integer, String, Text
from sqlalchemy import inspect as sa_inspect

revision = "20260820_04"
down_revision = "20260820_03"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    if "agent_tool_call" in sa_inspect(bind).get_table_names():
        return
    op.create_table(
        "agent_tool_call",
        Column("id", String, primary_key=True),
        Column("agent_id", String, nullable=True),
        Column("initiator_id", String, nullable=True),
        Column("provider_type", String, nullable=False),
        Column("provider_id", String, nullable=False),
        Column("tool_name", String, nullable=False),
        Column("status", String, nullable=False),
        Column("arguments_redacted", Text, nullable=False),
        Column("result_summary", Text, nullable=True),
        Column("error", Text, nullable=True),
        Column("duration_ms", Integer, nullable=True),
        Column("created_at", String, nullable=False),
        CheckConstraint(
            "status IN ('allowed','blocked','succeeded','failed')",
            name="ck_agent_tool_call_status",
        ),
        CheckConstraint(
            "provider_type IN ('native','mcp')",
            name="ck_agent_tool_call_provider_type",
        ),
    )
    op.create_index(
        "ix_agent_tool_call_created_at", "agent_tool_call", ["created_at"]
    )


def downgrade() -> None:
    if "agent_tool_call" in sa_inspect(op.get_bind()).get_table_names():
        op.drop_index("ix_agent_tool_call_created_at", table_name="agent_tool_call")
        op.drop_table("agent_tool_call")
