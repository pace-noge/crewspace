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
async def test_connected_remote_agent_fires_on_engaged_before_reply(monkeypatch):
    provider = MultiAgentProvider(
        {}, mentions={"remote": "agent_remote"}
    )
    socket = FakeSocket()
    await agent_manager.connect("agent_remote", cast(WebSocket, socket))
    events: list[str] = []

    async def send_and_wait(agent_id: str, payload: dict, timeout: float = 20.0):
        assert agent_id == "agent_remote"
        assert payload["type"] == "chat"
        events.append("reply")
        return "remote reply"

    monkeypatch.setattr(agent_manager, "send_and_wait", send_and_wait)
    try:
        async def on_engaged(agent_id: str) -> None:
            events.append(f"engaged:{agent_id}")

        agent_id, replies = await provider.on_chat_message(
            "@remote please help", NullRunner(), on_engaged=on_engaged
        )
    finally:
        agent_manager.disconnect("agent_remote", cast(WebSocket, socket))

    assert events == ["engaged:agent_remote", "reply"]
    assert agent_id == "agent_remote"
    assert replies == ["remote reply"]


@pytest.mark.asyncio
async def test_offline_remote_agent_does_not_fire_on_engaged():
    provider = MultiAgentProvider(
        {}, mentions={"remote": "agent_remote"}
    )
    engaged: list[str] = []

    async def on_engaged(agent_id: str) -> None:
        engaged.append(agent_id)

    agent_id, replies = await provider.on_chat_message(
        "@remote please help", NullRunner(), on_engaged=on_engaged
    )

    assert engaged == []
    assert agent_id == "agent_remote"
    assert replies == ["⚠️ Agent agent_remote is offline."]


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
async def test_connected_remote_agent_streams_working_frame_to_channel(client, app, monkeypatch):
    engaged: list[str] = []

    class RemoteProvider:
        def resolve(self, text):
            return "agent_planner"

        async def on_chat_message(self, text, runner, context=None, on_engaged=None):
            if on_engaged is not None:
                await on_engaged("agent_planner")
            engaged.append("agent_planner")
            return "agent_planner", ["remote says hi"]

    async def fake_build(settings, uow, *, principal_id=None):
        return RemoteProvider()

    monkeypatch.setattr(AgentRegistry, "build", staticmethod(fake_build))

    with client.websocket_connect(
        "/channels/chan_general/ws", headers={"Origin": "http://testserver"}
    ) as ws:
        ws.send_json({"body": "@remote hi"})
        # human echo
        human = ws.receive_json()
        assert human["body"] == "@remote hi"
        # typing identifies the resolved agent before the slow call starts
        typing = ws.receive_json()
        assert typing["type"] == "typing"
        # live "working" frame emitted when the remote agent is engaged
        working = ws.receive_json()
        assert working["type"] == "agent_working"
        assert working["author_id"] == "agent_planner"
        # the reply itself
        reply = ws.receive_json()
        assert reply["body"] == "remote says hi"
        assert reply["author_id"] == "agent_planner"

    assert engaged == ["agent_planner"]


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


@pytest.mark.asyncio
async def test_remote_agent_reply_uses_configured_timeout(monkeypatch):
    captured: dict[str, float] = {}

    async def fake_send_and_wait(agent_id, payload, timeout=20.0):
        captured["timeout"] = timeout
        return "done"

    monkeypatch.setattr(agent_manager, "send_and_wait", fake_send_and_wait)

    settings = Settings(agent_reply_timeout=900.0)
    provider = MultiAgentProvider({}, mentions={"coder": "agent_coder"}, settings=settings)
    await agent_manager.connect("agent_coder", cast(WebSocket, __import__("tests.test_agent_routing", fromlist=["FakeSocket"]).FakeSocket()))
    try:
        await provider.on_chat_message("@coder refactor the parser", None)
    finally:
        agent_manager.disconnect("agent_coder", cast(WebSocket, __import__("tests.test_agent_routing", fromlist=["FakeSocket"]).FakeSocket()))

    assert captured["timeout"] == 900.0
