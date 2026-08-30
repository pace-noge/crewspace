"""API: live board broadcast adapter (board_delta over WebSocket).

Composition-root helper that the API layer uses to turn an application-layer
board mutation into a canonical rendered fragment broadcast to the board room.
Deliberately imports only ``connection``, ``rendering``, ``dto.board`` and
``dto.mappers`` — never ``deps`` nor any router — so it can be imported from
dependency wiring without a circular import.
"""
from __future__ import annotations

import logging
from typing import Any

from ..application.tools import BoardDeltaPublisher
from ..domain.ports import UnitOfWork
from ..dto.board import BoardDeltaDTO
from ..dto.mappers import to_board
from .connection import manager
from .rendering import templates

_CARD_KINDS = frozenset({"card_created", "card_moved", "card_updated"})
logger = logging.getLogger(__name__)


def board_room(board_id: str) -> str:
    """Canonical room key for live viewers of one board."""
    return f"board:{board_id}"


def build_board_delta_publisher() -> BoardDeltaPublisher:
    """Adapter: render canonical fragments + broadcast to the board room.

    Used by the web composition roots (registry dep, agent WS) so agent-
    originated card/comment mutations are broadcast to live board viewers.
    Standalone MCP server keeps the default no-op publisher (no web process /
    ConnectionManager to broadcast to). The card fragment is rendered through
    the canonical ``card.html`` (with the board's columns for the dropdown),
    exactly like the HTTP broadcast paths.
    """

    async def publish(
        uow: UnitOfWork, board_id: str, delta: BoardDeltaDTO, payload: Any,
    ) -> None:
        try:
            if delta.kind in _CARD_KINDS:
                view = await uow.boards.get_board(board_id)
                board = to_board(view) if view else None
                html = templates.get_template("card.html").render(
                    card=payload, board_id=board_id, board=board
                )
                delta = delta.model_copy(update={"card_html": html})
            elif delta.kind == "comment_added":
                html = templates.get_template("comment.html").render(
                    comment=payload
                )
                delta = delta.model_copy(update={"comment_html": html})
            frame = {
                "type": "board_delta",
                "board_id": board_id,
                "delta": delta.model_dump(mode="json"),
            }
            await manager.broadcast(board_room(board_id), frame)
        except Exception:
            # Live transport must never roll back a mutation that already ran.
            # Fail-open here: the next canonical board load reconciles the UI.
            logger.exception("Failed to publish board delta for %s", board_id)

    return publish