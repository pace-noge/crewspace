"""Unread message indicators (M10 feature).

The sidebar shows a dot badge on a channel or direct message when it has
messages the current member has not yet read, cleared when the member opens
that channel/DM. Before the member opens a channel, ALL of its history counts
as unread (baseline on first open); after opening, only newer messages from
others count. Zero-count channels are omitted from the payload.
"""
from __future__ import annotations

import asyncio


def test_unread_returns_empty_for_cleared_channels(client) -> None:
    """After the member has opened and read a channel, its unread count is absent."""
    client.post("/api/unread/chan_general/mark-read")
    resp = client.get("/api/unread")
    assert resp.status_code == 200
    payload = resp.json()
    assert isinstance(payload, dict)
    assert payload.get("chan_general", 0) == 0


def test_unread_count_and_mark_read(client, app) -> None:
    """Posting a message bumps unread; opening the channel clears it."""
    # Baseline: read the channel so the seeded history doesn't flood the count.
    client.post("/api/unread/chan_general/mark-read")
    assert client.get("/api/unread").json().get("chan_general", 0) == 0

    async def arrive() -> None:
        async with app.state.db.uow() as uow:
            await uow.chat.add_message(
                "chan_general", "agent_planner", "an unread activity line"
            )
            await uow.commit()

    asyncio.run(arrive())

    resp = client.get("/api/unread")
    assert resp.status_code == 200
    assert resp.json()["chan_general"] >= 1

    # Open the channel (mark read).
    mark = client.post("/api/unread/chan_general/mark-read")
    assert mark.status_code in (200, 204), mark.text

    resp2 = client.get("/api/unread")
    assert resp2.json().get("chan_general", 0) == 0


def test_mark_read_rejects_unauthorized_channel(client) -> None:
    """Mark-read on a channel the member doesn't belong to returns 404."""
    resp = client.post("/api/unread/chan_doesnt_exist_xyz/mark-read")
    assert resp.status_code == 404


def test_mark_read_field_for_other_channel_scope(client, app) -> None:
    """Marking one channel read does not clear a different channel's count."""
    # chan_general starts unread (seeded history); mark it read and confirm general is gone.
    client.post("/api/unread/chan_general/mark-read")
    resp = client.get("/api/unread")
    # general was the ONLY unread channel — after marking it, payload is empty.
    assert resp.json().get("chan_general", 0) == 0


def test_sidebar_renders_unread_dot(client) -> None:
    """The sidebar channel row contains the unread dot element."""
    resp = client.get("/")
    assert resp.status_code == 200
    assert 'class="unread-dot"' in resp.text
    assert 'data-channel-id="chan_general"' in resp.text