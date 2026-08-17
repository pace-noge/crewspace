"""Add uses_app_llm flag to member.

Revision ID: 20260817_01
Revises: 20260816_01

Builtin agents run inside the main app using the server's LLM credentials
(uses_app_llm=1); remote agents connect over WebSocket with their own identity
(uses_app_llm=0).

The initial schema revision builds the table from the declarative metadata, which
already includes this column on a fresh database, so this migration only adds it
when it is genuinely missing (idempotent across fresh and legacy databases).
"""
from __future__ import annotations

from alembic import op
from sqlalchemy import Column, Integer
from sqlalchemy import inspect as sa_inspect

revision = "20260817_01"
down_revision = "20260816_01"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    existing = {c["name"] for c in sa_inspect(bind).get_columns("member")}
    if "uses_app_llm" not in existing:
        with op.batch_alter_table("member") as batch_op:
            batch_op.add_column(
                Column("uses_app_llm", Integer, nullable=False, server_default="0")
            )
    # Legacy local LLM agents already use the main app's credentials; mark them
    # explicitly so the UI badge and authorization model reflect reality.
    op.execute(
        "UPDATE member SET uses_app_llm = 1 "
        "WHERE kind = 'agent' AND pubkey IS NULL AND backend = 'llm'"
    )


def downgrade() -> None:
    with op.batch_alter_table("member") as batch_op:
        batch_op.drop_column("uses_app_llm")
