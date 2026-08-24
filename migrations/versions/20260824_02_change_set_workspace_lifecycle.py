"""Allow pending and completed remote workspace lifecycle states."""
from __future__ import annotations

from alembic import op
from sqlalchemy import inspect as sa_inspect

revision = "20260824_02"
down_revision = "20260824_01"
branch_labels = None
depends_on = None

_OLD_STATUS_CHECK = (
    "status IN ('captured','reviewed','pr_requested','retained','discard_requested')"
)
_NEW_STATUS_CHECK = (
    "status IN ('captured','reviewed','pr_requested','retain_requested',"
    "'retained','discard_requested','discarded')"
)


def _has_change_sets() -> bool:
    return "stored_change_set" in sa_inspect(op.get_bind()).get_table_names()


def upgrade() -> None:
    if not _has_change_sets():
        return
    with op.batch_alter_table("stored_change_set") as batch:
        batch.drop_constraint("ck_stored_change_set_status", type_="check")
        batch.create_check_constraint("ck_stored_change_set_status", _NEW_STATUS_CHECK)


def downgrade() -> None:
    if not _has_change_sets():
        return
    op.execute(
        "UPDATE stored_change_set SET status='reviewed' "
        "WHERE status IN ('retain_requested','discarded')"
    )
    with op.batch_alter_table("stored_change_set") as batch:
        batch.drop_constraint("ck_stored_change_set_status", type_="check")
        batch.create_check_constraint("ck_stored_change_set_status", _OLD_STATUS_CHECK)
