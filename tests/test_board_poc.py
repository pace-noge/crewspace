"""M7.7 — board operating-surface integration POC (RED first).

run_board_poc drives the WHOLE M7 stack end to end on an isolated seeded DB
(the app fixture's temp DB): create a board, add a card with metadata, link it
to a coding run, move it into a trigger column to start a workflow, comment,
observe live board-room deltas, and confirm attention items surface in the
team-scoped inbox (with zero cross-tenant leakage). It is deterministic
(fixed seeded ids, stubbed agent transport) and isolated (temp DB, no
production workspace).
"""
from __future__ import annotations


def test_board_poc_walks_the_operating_surface(app):
    from crewspace.application.board_poc import run_board_poc

    report = run_board_poc(app)

    # 1. Board + card created with editable metadata.
    assert report.board_id.startswith("board_")
    assert report.column_count == 3
    assert report.card_title == "POC task"
    assert report.card_priority == "high"
    assert report.card_labels == ("poc",)
    assert report.card_activity_count >= 1  # metadata edits leave an audit trail

    # 2. A coding run is linked to the card and shows live status.
    assert report.linked_run_id is not None
    assert report.linked_run_status == "running"

    # 3. Moving the card into the trigger column starts a workflow run.
    assert report.moved_column is not None
    assert report.workflow_run is not None  # a column_move run was enqueued

    # 4. A comment is persisted (the surface may add auxiliary comments, so
    #    assert at-least-one rather than an exact count).
    assert report.comment_count >= 1

    # 5. Live deltas reach the board room (a second viewer's WS subscription).
    assert set(report.live_delta_kinds) == {"card_created", "card_moved", "comment_added"}

    # 6. Attention items surface in the team inbox with zero cross-tenant leakage.
    assert set(report.inbox_kinds) >= {"workflow_failed", "stale_task"}
    assert report.cross_tenant_visible == 0


def test_board_poc_is_deterministic_and_isolated(app):
    """Two fresh, isolated DBs produce identical observable behavior.

    Determinism is asserted over behavior (counts, kinds, statuses) and the
    POC-fixed ids (run/workflow/repo); service-generated board/card ids are by
    design unique per run and are not asserted for identity.
    """
    from fastapi import FastAPI

    from crewspace.application.board_poc import run_board_poc
    from crewspace.config import Settings
    from crewspace.infrastructure.db import Database
    from crewspace.main import create_app
    import asyncio, tempfile
    from pathlib import Path

    def fresh_app() -> FastAPI:
        tmp = Path(tempfile.mkdtemp())
        settings = Settings(db_path=str(tmp / "test.db"))
        database = asyncio.run(Database.create(settings))
        application = create_app()
        application.state.db = database
        application.state.settings = settings
        application.state.start_schedulers = False
        return application

    a1, a2 = fresh_app(), fresh_app()
    first, second = run_board_poc(a1), run_board_poc(a2)
    try:
        assert first.column_count == second.column_count
        assert first.card_title == second.card_title
        assert first.card_priority == second.card_priority
        assert first.card_labels == second.card_labels
        assert first.card_activity_count == second.card_activity_count
        assert first.linked_run_id == second.linked_run_id
        assert first.linked_run_status == second.linked_run_status
        # workflow run ids are service-generated (unique per run); assert the
        # presence of a triggered run on both isolated DBs, not id equality.
        assert first.workflow_run is not None and second.workflow_run is not None
        # column ids are service-generated per board (unique per run); assert
        # the move occurred, not column id equality.
        assert first.moved_column is not None and second.moved_column is not None
        assert first.comment_count == second.comment_count
        assert first.live_delta_kinds == second.live_delta_kinds
        assert first.inbox_kinds == second.inbox_kinds
        assert first.cross_tenant_visible == second.cross_tenant_visible
    finally:
        for application in (a1, a2):
            if not getattr(application.state, "db_closed_by_lifespan", False):
                asyncio.run(application.state.db.close())
