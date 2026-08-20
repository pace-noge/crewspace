"""Agent routing and transaction-boundary regressions."""
from __future__ import annotations

from typing import Any, cast

import pytest
from fastapi import WebSocket

from crewspace.api.connection import agent_manager
from crewspace.application.services import ChatService
from crewspace.application.tools import build_registry
from crewspace.config import Settings
from crewspace.domain.entities import CardView
from crewspace.infrastructure.agents.registry import AgentRegistry, MultiAgentProvider


class RecordingAgent:
    name = "Planner"

    def __init__(self) -> None:
        self.chat_calls = 0
        self.card_calls = 0

    async def on_chat_message(self, text, runner):
        self.chat_calls += 1
        return "agent_planner", ["local reply"]

    async def on_card_created(self, card, runner):
        self.card_calls += 1


class FakeSocket:
    def __init__(self) -> None:
        self.sent: list[dict] = []

    async def accept(self) -> None:
        pass

    async def send_json(self, payload: dict) -> None:
        self.sent.append(payload)


class NullRunner:
    async def run(self, tool_name: str, **args: Any) -> dict:
        return {}


@pytest.mark.asyncio
async def test_unmentioned_chat_does_not_route_to_connected_default_agent():
    local = RecordingAgent()
    provider = MultiAgentProvider({"agent_planner": local})
    socket = FakeSocket()
    await agent_manager.connect("agent_planner", cast(WebSocket, socket))
    try:
        agent_id, replies = await provider.on_chat_message("ordinary channel chat", NullRunner())
    finally:
        agent_manager.disconnect("agent_planner", cast(WebSocket, socket))

    assert (agent_id, replies) == ("", [])
    assert socket.sent == []
    assert local.chat_calls == 0


@pytest.mark.asyncio
async def test_disconnected_remote_agent_does_not_use_local_fallback():
    provider = MultiAgentProvider(
        {}, mentions={"remote": "agent_remote"}
    )

    agent_id, replies = await provider.on_chat_message(
        "@remote please help", NullRunner()
    )

    assert agent_id == "agent_remote"
    assert replies == ["⚠️ Agent agent_remote is offline."]


@pytest.mark.asyncio
async def test_connected_agent_receives_card_event_only_once():
    local = RecordingAgent()
    provider = MultiAgentProvider({"agent_planner": local})
    socket = FakeSocket()
    await agent_manager.connect("agent_planner", cast(WebSocket, socket))
    card = CardView("card_1", "col_todo", "One event", None, None, 0)
    try:
        await provider.on_card_created(card, NullRunner())
    finally:
        agent_manager.disconnect("agent_planner", cast(WebSocket, socket))

    assert len(socket.sent) == 1
    assert socket.sent[0]["type"] == "card_created"
    assert local.card_calls == 0


@pytest.mark.asyncio
async def test_chat_commits_human_message_before_waiting_for_agent(app, monkeypatch):
    observed: dict[str, bool] = {}

    class Provider:
        def resolve(self, text):
            return None

        async def on_chat_message(self, text, runner, context=None):
            async with app.state.db.uow() as other:
                row = await (
                    await other._conn.execute(
                        "SELECT COUNT(*) AS n FROM message WHERE body='Committed before wait'"
                    )
                ).fetchone()
                observed["visible"] = row["n"] == 1
            return "", []

    async def fake_build(settings, uow, *, principal_id=None):
        assert principal_id == "user_bilal"
        return Provider()

    monkeypatch.setattr(AgentRegistry, "build", staticmethod(fake_build))
    async with app.state.db.uow() as uow:
        service = ChatService(build_registry(), Settings(db_path=app.state.settings.db_path))
        await service.post_and_respond(
            "chan_general", "user_bilal", "Committed before wait", uow
        )

    assert observed["visible"] is True
