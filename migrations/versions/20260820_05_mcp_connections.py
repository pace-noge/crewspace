"""Add external MCP connections and discovered tools.

Revision ID: 20260820_05
Revises: 20260820_04
"""
from __future__ import annotations

from alembic import op
from sqlalchemy import CheckConstraint, Column, ForeignKey, Integer, String, Text
from sqlalchemy import UniqueConstraint, inspect as sa_inspect

revision = "20260820_05"
down_revision = "20260820_04"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    tables = set(sa_inspect(bind).get_table_names())
    if "mcp_connection" not in tables:
        op.create_table(
            "mcp_connection",
            Column("id", String, primary_key=True),
            Column("name", String, nullable=False),
            Column("namespace", String, nullable=False),
            Column("transport", String, nullable=False),
            Column("endpoint_or_command", Text, nullable=False),
            Column("enabled", Integer, nullable=False, server_default="0"),
            Column("auth_secret_ref", Text, nullable=True),
            Column("created_by", String, nullable=False),
            Column("created_at", String, nullable=False),
            Column("updated_at", String, nullable=False),
            UniqueConstraint("namespace", name="uq_mcp_connection_namespace"),
            CheckConstraint(
                "transport IN ('streamable_http','sse','stdio_managed')",
                name="ck_mcp_connection_transport",
            ),
            CheckConstraint("enabled IN (0,1)", name="ck_mcp_connection_enabled"),
        )
    if "mcp_discovered_tool" not in tables:
        op.create_table(
            "mcp_discovered_tool",
            Column(
                "connection_id",
                String,
                ForeignKey("mcp_connection.id", ondelete="CASCADE"),
                primary_key=True,
            ),
            Column("tool_name", String, primary_key=True),
            Column("description", Text, nullable=False),
            Column("input_schema", Text, nullable=False),
            Column("schema_hash", String, nullable=False),
            Column("approval_state", String, nullable=False, server_default="pending"),
            Column("discovered_at", String, nullable=False),
            CheckConstraint(
                "approval_state IN ('pending','approved','changed','disabled')",
                name="ck_mcp_discovered_tool_approval_state",
            ),
        )


def downgrade() -> None:
    tables = set(sa_inspect(op.get_bind()).get_table_names())
    if "mcp_discovered_tool" in tables:
        op.drop_table("mcp_discovered_tool")
    if "mcp_connection" in tables:
        op.drop_table("mcp_connection")
