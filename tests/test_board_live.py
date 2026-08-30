"""M7.3 — Live board updates over WebSocket (RED phase, vertical slice).

Board mutations (create/move/comment/edit) must publish a minimal
`board_delta` frame to a board-scoped room (`board:{board_id}`), so other
viewers of that board update the card in place without a page reload, while
the acting client keeps its canonical whole-board feedback.

Server-side surface (this slice):
  - A `BoardDeltaDTO` (pure) describing the minimal mutation for the wire.
  - A board WebSocket endpoint `/boards/{board_id}/ws` that authorizes the
    member for that board and joins room `board:{board_id}`.
  - Mutations broadcast `{"type": "board_delta", "board_id": ..., "delta": {...}}`
    into the board room. Board-scoped: members of other boards/workspaces get
    nothing.
"""
from __future__ import annotations

import asyncio
import re

import pytest

from starlette.websockets import WebSocketDisconnect

from crewspace.api.board_live import build_board_delta_publisher
from crewspace.api.connection import ConnectionManager, manager
from crewspace.application.tools import build_registry


# ---------------------------------------------------------------------------
# Server-side: a board WebSocket endpoint + board_delta broadcast
# ---------------------------------------------------------------------------


def test_connection_manager_board_rooms_are_isolated():
    """Broadcasting board:A never emits to a socket subscribed to board:B."""

    class FakeWebSocket:
        def __init__(self) -> None:
            self.accepted = False
            self.frames: list[dict] = []

        async def accept(self) -> None:
            self.accepted = True

        async def send_json(self, payload: dict) -> None:
            self.frames.append(payload)

    async def exercise() -> tuple[FakeWebSocket, FakeWebSocket]:
        room_manager = ConnectionManager()
        board_a = FakeWebSocket()
        board_b = FakeWebSocket()
        await room_manager.connect("board:A", board_a)  # type: ignore[arg-type]
        await room_manager.connect("board:B", board_b)  # type: ignore[arg-type]
        await room_manager.broadcast("board:A", {"type": "board_delta", "board_id": "A"})
        return board_a, board_b

    board_a, board_b = asyncio.run(exercise())
    assert board_a.frames == [{"type": "board_delta", "board_id": "A"}]
    assert board_b.frames == []


def test_board_ws_rejects_anonymous_visitor(anonymous_client):
    """A visitor with no valid session may not subscribe to a board room;
    the endpoint must close the socket (fail-closed authz)."""
    with pytest.raises(WebSocketDisconnect) as exc_info:
        with anonymous_client.websocket_connect(
            "/boards/board_main/ws", headers={"Origin": "http://testserver"}
        ):
            pass
    assert exc_info.value.code == 4003


def test_two_viewers_get_board_delta_on_card_create(client):
    """Two members viewing the same board: one creates a card, the other
    receives a board_delta frame with kind=card_created and the card title.
    (Creation may also publish an agent comment delta first, so scan for the
    expected kind rather than assuming arrival order.)"""

    def frames_until(ws, kind):
        frames = []
        while True:
            frame = ws.receive_json()
            frames.append(frame)
            if frame["type"] == "board_delta" and frame["delta"]["kind"] == kind:
                return frame, frames

    with client.websocket_connect(
        "/boards/board_main/ws", headers={"Origin": "http://testserver"}
    ) as viewer:
        with client.websocket_connect(
            "/boards/board_main/ws", headers={"Origin": "http://testserver"}
        ) as actor:
            posted = client.post(
                "/boards/board_main/cards",
                data={"column_id": "col_todo", "title": "Live card"},
            )
            assert posted.status_code == 200
            frame, _ = frames_until(viewer, "card_created")
            assert frame["type"] == "board_delta"
            assert frame["board_id"] == "board_main"
            assert frame["delta"]["kind"] == "card_created"
            # The delta carries enough to update the card in place.
            assert frame["delta"]["card_id"]
            assert "Live card" in frame["delta"]["title"]


def test_board_delta_carries_kind_card_moved(client):
    """A move publishes kind=card_moved with from/to column ids.
    (Scan for the expected kind; creation may also publish an agent comment.)"""

    def frames_until(ws, kind):
        frames = []
        while True:
            frame = ws.receive_json()
            frames.append(frame)
            if frame["type"] == "board_delta" and frame["delta"]["kind"] == kind:
                return frame, frames

    with client.websocket_connect(
        "/boards/board_main/ws", headers={"Origin": "http://testserver"}
    ) as viewer:
        made = client.post(
            "/boards/board_main/cards",
            data={"column_id": "col_todo", "title": "Move me"},
        )
        assert made.status_code == 200
        match = re.search(r'id="card-([A-Za-z0-9_-]+)"', made.text)
        assert match is not None
        card_id = match.group(1)
        moved = client.post(
            f"/cards/{card_id}/move", data={"column_id": "col_doing"}
        )
        assert moved.status_code == 200
        # The viewer sees the move delta (create may be interleaved with
        # an agent-comment delta, so scan to the move).
        move_frame, _ = frames_until(viewer, "card_moved")
        assert move_frame["delta"]["kind"] == "card_moved"
        assert move_frame["delta"]["card_id"] == card_id
        assert move_frame["delta"]["from_column_id"] == "col_todo"
        assert move_frame["delta"]["to_column_id"] == "col_doing"


def test_board_delta_carries_kind_card_updated(client):
    """A card edit publishes kind=card_updated with the changed fields.
    (Scan for the expected kind; creation may also publish an agent comment.)"""

    def frames_until(ws, kind):
        frames = []
        while True:
            frame = ws.receive_json()
            frames.append(frame)
            if frame["type"] == "board_delta" and frame["delta"]["kind"] == kind:
                return frame, frames

    with client.websocket_connect(
        "/boards/board_main/ws", headers={"Origin": "http://testserver"}
    ) as viewer:
        made = client.post(
            "/boards/board_main/cards",
            data={"column_id": "col_todo", "title": "Edit me"},
        )
        assert made.status_code == 200
        match = re.search(r'id="card-([A-Za-z0-9_-]+)"', made.text)
        card_id = match.group(1) if match else ""
        edited = client.post(
            f"/boards/board_main/cards/{card_id}",
            data={
                "title": "Edited title",
                "description": "New description",
                "priority": "high",
                "labels": "",
            },
        )
        assert edited.status_code == 200
        # The viewer sees the update delta (create may be interleaved with
        # an agent-comment delta, so scan to the update).
        update_frame, _ = frames_until(viewer, "card_updated")
        assert update_frame["delta"]["kind"] == "card_updated"
        assert update_frame["delta"]["card_id"] == card_id
        assert update_frame["delta"]["title"] == "Edited title"


# ---------------------------------------------------------------------------
# Agent-originated mutations must also broadcast (reviewer blocker: wiring gap)
# ---------------------------------------------------------------------------


async def test_agent_tool_create_card_publishes_board_delta(app):
    """An agent calling create_card via a bound runner must publish a
    board_delta so other viewers see the agent's card live."""
    published: list[dict] = []

    async def capture(uow, board_id: str, delta, payload) -> None:
        published.append({"board_id": board_id, "delta": delta, "payload": payload})

    async with app.state.db.uow() as uow:
        runner = build_registry(publisher=capture).bind_trusted(uow)
        result = await runner.run("create_card", column_id="col_todo", title="Agent card")
        assert result["column_id"] == "col_todo"

    assert published, "agent create_card must publish a board_delta"
    assert published[0]["board_id"] == "board_main"
    assert published[0]["delta"].kind == "card_created"
    assert published[0]["delta"].card_id == result["id"]


async def test_agent_tool_move_card_publishes_board_delta(app):
    """An agent moving a card must publish a card_moved delta."""
    published: list[dict] = []

    async def capture(uow, board_id: str, delta, payload) -> None:
        published.append({"board_id": board_id, "delta": delta, "payload": payload})

    async with app.state.db.uow() as uow:
        runner = build_registry(publisher=capture).bind_trusted(uow)
        result = await runner.run("create_card", column_id="col_todo", title="Move agent card")
        card_id = result["id"]
        moved = await runner.run("move_card", card_id=card_id, column_id="col_doing")
        assert moved["column_id"] == "col_doing"

    kinds = [p["delta"].kind for p in published]
    assert kinds == ["card_created", "card_moved"]
    assert published[1]["board_id"] == "board_main"
    assert published[1]["delta"].card_id == card_id


async def test_agent_tool_comment_card_publishes_board_delta(app):
    """An agent commenting on a card must publish a comment_added delta."""
    published: list[dict] = []

    async def capture(uow, board_id: str, delta, payload) -> None:
        published.append({"board_id": board_id, "delta": delta, "payload": payload})

    async with app.state.db.uow() as uow:
        runner = build_registry(publisher=capture).bind_trusted(uow)
        made = await runner.run("create_card", column_id="col_todo", title="Comment agent card")
        card_id = made["id"]
        commented = await runner.run("comment_card", card_id=card_id, body="Nice card")
        assert commented["body"] == "Nice card"

    kinds = [p["delta"].kind for p in published]
    assert kinds == ["card_created", "comment_added"]
    assert published[1]["board_id"] == "board_main"
    assert published[1]["delta"].card_id == card_id


async def test_agent_tool_update_card_publishes_board_delta(app):
    """An agent updating a card must publish a card_updated delta."""
    published: list[dict] = []

    async def capture(uow, board_id: str, delta, payload) -> None:
        published.append({"board_id": board_id, "delta": delta, "payload": payload})

    async with app.state.db.uow() as uow:
        runner = build_registry(publisher=capture).bind_trusted(uow)
        made = await runner.run("create_card", column_id="col_todo", title="Update agent card")
        card_id = made["id"]
        updated = await runner.run(
            "update_card", card_id=card_id, title="Updated agent card", priority="high"
        )
        assert updated and updated["title"] == "Updated agent card"

    kinds = [p["delta"].kind for p in published]
    assert kinds == ["card_created", "card_updated"]
    assert published[1]["board_id"] == "board_main"
    assert published[1]["delta"].card_id == card_id


async def test_web_agent_tool_publisher_renders_canonical_card_fragment(
    app, monkeypatch: pytest.MonkeyPatch
):
    """The real web composition adapter must render card_html and target the
    canonical board room; a capture-only publisher would miss template errors."""
    broadcasts: list[tuple[str, dict]] = []

    async def capture_broadcast(room: str, frame: dict) -> None:
        broadcasts.append((room, frame))

    monkeypatch.setattr(manager, "broadcast", capture_broadcast)
    async with app.state.db.uow() as uow:
        runner = build_registry(
            publisher=build_board_delta_publisher()
        ).bind_trusted(uow)
        result = await runner.run(
            "create_card", column_id="col_todo", title="Rendered agent card"
        )

    assert len(broadcasts) == 1
    room, frame = broadcasts[0]
    assert room == "board:board_main"
    assert frame["type"] == "board_delta"
    assert frame["delta"]["kind"] == "card_created"
    assert frame["delta"]["card_id"] == result["id"]
    card_html = frame["delta"]["card_html"]
    assert f'id="card-{result["id"]}"' in card_html
    assert "Rendered agent card" in card_html
    # Agent deltas use the same canonical card fragment as HTTP routes, so
    # replacing a remote viewer's card does not remove its interactions.
    assert f'hx-post="/cards/{result["id"]}/move"' in card_html
    assert f'hx-post="/cards/{result["id"]}/comments"' in card_html