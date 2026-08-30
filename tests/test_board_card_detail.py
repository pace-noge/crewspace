"""M7.1 — card detail view and edit (RED phase).

These tests assert the user-visible feature BEFORE any production code exists:
  - a card detail route renders metadata fields (description, assignee, due, priority, labels)
  - BoardService.update_card persists description/due_date/priority/labels
  - BoardService.set_assignee persists the assignee
  - every edit writes a card_activity audit record
  - the detail route 404s for an unknown card
  - the persistence layer returns the new fields through the view model
"""
from __future__ import annotations

import re

from crewspace.application.services import BoardService
from crewspace.application.tools import build_registry
from crewspace.config import Settings


def _create_card(client) -> str:
    r = client.post(
        "/boards/board_main/cards",
        data={"column_id": "col_todo", "title": "M7.1 detail card"},
    )
    assert r.status_code == 200
    m = re.search(r'id="card-([0-9a-f]+)"', r.text)
    assert m, f"no card id in response: {r.text[:200]}"
    return m.group(1)


_SVC = BoardService(build_registry(), Settings())


def test_card_detail_page_renders_metadata_fields(client):
    card_id = _create_card(client)
    r = client.get(f"/boards/board_main/cards/{card_id}")
    assert r.status_code == 200
    # The detail surface must surface the editable metadata the board omits today.
    assert 'name="description"' in r.text
    assert 'name="due_date"' in r.text
    assert 'name="priority"' in r.text
    assert 'name="labels"' in r.text
    assert 'name="assignee_id"' in r.text


def test_card_detail_page_404_for_unknown(client):
    r = client.get("/boards/board_main/cards/does-not-exist")
    assert r.status_code == 404


async def test_update_card_persists_metadata(client, app):
    card_id = _create_card(client)
    async with app.state.db.uow() as uow:
        await _SVC.update_card(
            card_id,
            uow,
            actor_id="user_bilal",
            description="Implement the login flow",
            due_date="2026-09-15",
            priority="high",
            labels=["backend", "auth"],
        )
        card = await uow.boards.get_card(card_id)
    assert card is not None
    assert card.description == "Implement the login flow"
    assert card.due_date == "2026-09-15"
    assert card.priority == "high"
    assert set(card.labels) == {"backend", "auth"}


async def test_set_assignee_persists(client, app):
    card_id = _create_card(client)
    async with app.state.db.uow() as uow:
        await _SVC.set_assignee(card_id, "user_bilal", uow, actor_id="user_bilal")
        card = await uow.boards.get_card(card_id)
    assert card is not None
    assert card.assignee_id == "user_bilal"
    assert card.assignee_name == "Bilal"


async def test_card_update_records_activity(client, app):
    card_id = _create_card(client)
    async with app.state.db.uow() as uow:
        await _SVC.update_card(
            card_id, uow, actor_id="user_bilal", description="Now with a description", priority="low"
        )
        activity = await uow.boards.list_card_activity(card_id)
    assert activity, "update must write at least one card_activity record"
    kinds = {a.field for a in activity}
    assert "description" in kinds
    assert "priority" in kinds
    for a in activity:
        assert a.actor_id == "user_bilal"


async def test_set_assignee_unchanged_writes_no_activity(client, app):
    card_id = _create_card(client)
    async with app.state.db.uow() as uow:
        await _SVC.set_assignee(card_id, "user_bilal", uow, actor_id="user_bilal")
        first = await uow.boards.list_card_activity(card_id)
        # Same assignee, save again — must not create a second activity row.
        await _SVC.set_assignee(card_id, "user_bilal", uow, actor_id="user_bilal")
        second = await uow.boards.list_card_activity(card_id)
    assert len(second) == len(first)


async def test_update_card_rejects_empty_title(app):
    async with app.state.db.uow() as uow:
        card = await uow.boards.add_card("col_todo", "Title guard")
        try:
            await _SVC.update_card(card.id, uow, actor_id="user_bilal", title="")
        except ValueError:
            pass
        else:
            raise AssertionError("empty title must be rejected")
        refreshed = await uow.boards.get_card(card.id)
        assert refreshed is not None and refreshed.title == "Title guard"


def test_card_detail_form_save_round_trip(client):
    card_id = _create_card(client)
    r = client.post(
        f"/boards/board_main/cards/{card_id}",
        data={
            "title": "Renamed card",
            "description": "Full description **with markdown**",
            "assignee_id": "user_bilal",
            "due_date": "2026-09-20",
            "priority": "urgent",
            "labels": "backend, auth",
        },
    )
    assert r.status_code == 200
    assert "Renamed card" in r.text
    assert 'name="description"' in r.text
    assert 'value="2026-09-20"' in r.text
    assert "urgent" in r.text.lower()
    assert "backend" in r.text
    assert "auth" in r.text


def test_card_detail_form_can_clear_optional_metadata(client):
    card_id = _create_card(client)
    populated = {
        "title": "Clear metadata",
        "description": "temporary",
        "assignee_id": "",
        "due_date": "2026-09-20",
        "priority": "high",
        "labels": "temporary",
    }
    assert client.post(f"/boards/board_main/cards/{card_id}", data=populated).status_code == 200

    cleared = client.post(
        f"/boards/board_main/cards/{card_id}",
        data={
            "title": "Clear metadata",
            "description": "",
            "assignee_id": "",
            "due_date": "",
            "priority": "",
            "labels": "",
        },
    )
    assert cleared.status_code == 200
    detail = client.get(f"/boards/board_main/cards/{card_id}")
    assert '<textarea name="description" rows="7" style="width:100%;"></textarea>' in detail.text
    assert 'name="due_date" value=""' in detail.text
    assert 'name="labels" value=""' in detail.text


def test_card_detail_form_rejects_bad_priority(client):
    card_id = _create_card(client)
    r = client.post(
        f"/boards/board_main/cards/{card_id}",
        data={
            "title": "Bad priority",
            "priority": "bogus",
            "labels": "",
        },
    )
    assert r.status_code == 422


def test_card_detail_form_rejects_unknown_assignee(client):
    card_id = _create_card(client)
    r = client.post(
        f"/boards/board_main/cards/{card_id}",
        data={
            "title": "Bad assignee",
            "assignee_id": "does-not-exist",
            "labels": "",
        },
    )
    assert r.status_code == 422


def test_card_detail_page_404_for_card_in_other_board(client):
    r = client.get("/boards/board_main/cards/does-not-exist")
    assert r.status_code == 404
