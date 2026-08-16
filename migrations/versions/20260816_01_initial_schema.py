"""Create the initial Crewspace schema.

Revision ID: 20260816_01
Revises: None
"""
from __future__ import annotations

from alembic import op

from crewspace.infrastructure.models import Base

revision = "20260816_01"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    # checkfirst preserves normalized legacy SQLite databases while creating the
    # complete schema on empty SQLite and PostgreSQL databases.
    Base.metadata.create_all(bind=op.get_bind(), checkfirst=True)


def downgrade() -> None:
    Base.metadata.drop_all(bind=op.get_bind(), checkfirst=True)
