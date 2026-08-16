"""M1: real LLM agent — function calling over the Tool Registry.

These tests mock the OpenAI client (no network, no API key) by injecting a
fake client through LLMAgent's `client_factory`. We assert that:
  * the agent executes the LLM-requested tool calls through the registry runner,
  * it returns the model's final natural-language reply,
  * it ignores messages that don't @mention the agent (no LLM call).
The live WebSocket path is covered separately in test_app.py via StubAgent;
here we exercise the LLMAgent in isolation with a scripted tool runner.
"""
from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from typing import Any

from openai.types.chat import ChatCompletion, ChatCompletionMessage
from openai.types.chat.chat_completion_message_tool_call import (
    ChatCompletionMessageToolCall,
)
from openai.types.completion_usage import CompletionUsage

from crewspace.application.tools import build_registry
from crewspace.domain.identifiers import DEFAULT_BOARD_ID
from crewspace.infrastructure.agents.llm import LLMAgent
from crewspace.domain.ports import ToolRunner


# --- fake OpenAI client -----------------------------------------------------

class _ScriptedCompletions:
    """A scripted chat.completions.create. `script` is popped in order."""

    def __init__(self, script: list[ChatCompletion], calls: list[dict[str, Any]]) -> None:
        self._script = list(script)
        self.calls = calls

    async def create(self, **kwargs: Any) -> ChatCompletion:
        self.calls.append(kwargs)
        if not self._script:
            raise AssertionError("fake client out of scripted responses")
        return self._script.pop(0)


class _ScriptedClient:
    """Drop-in fake for AsyncOpenAI: accepts (api_key, base_url) and serves a
    scripted list of ChatCompletions via `.chat.completions.create`."""

    def __init__(self, script: list[ChatCompletion]) -> None:
        self.calls: list[dict[str, Any]] = []
        self._completions = _ScriptedCompletions(script, self.calls)

    @property
    def chat(self) -> Any:
        return type("Chat", (), {"completions": self._completions})()


# --- response builders ------------------------------------------------------

def _msg(content: str | None, tool_calls: list[Any] | None) -> ChatCompletionMessage:
    return ChatCompletionMessage.model_construct(
        role="assistant", content=content, tool_calls=tool_calls, refusal=None
    )


def _completion(message: ChatCompletionMessage) -> ChatCompletion:
    return ChatCompletion.model_construct(
        id="cmpl_test",
        model="test-model",
        object="chat.completion",
        created=0,
        choices=[{"index": 0, "finish_reason": "stop", "message": message}],
        usage=CompletionUsage.model_construct(prompt_tokens=1, completion_tokens=1, total_tokens=2),
    )


def _tool_call(call_id: str, name: str, arguments: dict) -> ChatCompletionMessageToolCall:
    fn = {"name": name, "arguments": json.dumps(arguments)}
    return ChatCompletionMessageToolCall.model_construct(id=call_id, type="function", function=fn)


# --- tool-runner stub -------------------------------------------------------

class _BoundRunnerStub(ToolRunner):
    """Implements ToolRunner with a custom async function (no DB)."""

    def __init__(self, fn: Callable[[str, Any], Any]) -> None:
        self._fn = fn

    async def run(self, name: str, **args: Any) -> dict:
        return await self._fn(name, **args)


def _make_agent(script: list[ChatCompletion]) -> LLMAgent:
    return LLMAgent.from_registry(
        build_registry(),
        api_key="test",
        model="test-model",
        client_factory=lambda *a, **k: _ScriptedClient(script),
    )


# --- tests ------------------------------------------------------------------

def test_llm_agent_creates_card_via_tool():
    """Agent calls create_card from a tool-call, then returns a text reply."""
    script = [
        _completion(_msg(None, [_tool_call("c1", "create_card", {"column_id": "col_todo", "title": "LLM card"})])),
        _completion(_msg("Done — created «LLM card» in To Do.", None)),
    ]
    agent = _make_agent(script)
    calls: dict[str, Any] = {}

    async def runner(name: str, **args: Any) -> dict:
        calls[name] = args
        if name == "create_card":
            return {"id": "card_llm", "title": args["title"], "column_id": args["column_id"]}
        return {}

    agent_id, out = asyncio.run(agent.on_chat_message("@planner make a card", _BoundRunnerStub(runner)))

    assert "create_card" in calls, "agent must have called create_card"
    assert calls["create_card"]["title"] == "LLM card"
    assert calls["create_card"]["column_id"] == "col_todo"
    assert any("LLM card" in r for r in out), f"reply should mention the card: {out}"


def test_llm_agent_ignores_non_mentions():
    """Without an @planner mention the agent stays silent (no LLM call)."""
    agent = _make_agent([])

    async def runner(name: str, **args: Any) -> dict:  # pragma: no cover
        raise AssertionError("LLM should not be called")

    out = asyncio.run(agent.on_chat_message("just chatting, no mention", _BoundRunnerStub(runner)))
    _aid, replies = out
    assert replies == []


def test_llm_agent_move_then_reply():
    """Two-step: find_card then move_card, then a final reply."""
    script = [
        _completion(_msg(None, [_tool_call("c1", "find_card", {"board_id": DEFAULT_BOARD_ID, "title": "Wire websocket chat"})])),
        _completion(_msg(None, [_tool_call("c2", "move_card", {"card_id": "card_found", "column_id": "col_done"})])),
        _completion(_msg("Moved it to Done 🎉", None)),
    ]
    agent = _make_agent(script)
    seen: dict[str, Any] = {}

    async def runner(name: str, **args: Any) -> dict:
        seen[name] = args
        if name == "find_card":
            return {"id": "card_found", "title": args["title"], "column_id": "col_doing"}
        if name == "move_card":
            return {"id": args["card_id"], "title": "Wire websocket chat", "column_id": args["column_id"]}
        return {}

    agent_id, out = asyncio.run(agent.on_chat_message("@planner move that card to done", _BoundRunnerStub(runner)))
    assert "find_card" in seen and "move_card" in seen
    assert seen["move_card"]["column_id"] == "col_done"
    assert any("Done" in r for r in out)


def test_llm_agent_passes_function_definitions():
    """The OpenAI request must carry the registry tools as functions."""
    async def runner(name: str, **args: Any) -> dict:
        return {}

    client = _ScriptedClient([_completion(_msg("ok", None))])
    agent2 = LLMAgent.from_registry(
        build_registry(), api_key="test", model="test-model",
        client_factory=lambda *a, **k: client,
    )
    asyncio.run(agent2.on_chat_message("@planner hi", _BoundRunnerStub(runner)))
    tools = client.calls[0]["tools"]
    names = {t["function"]["name"] for t in tools}
    assert {"create_card", "move_card", "comment_card", "find_card", "list_columns", "post_message"} <= names
    # Every tool must be a valid object schema with a parameters object.
    for t in tools:
        assert t["type"] == "function"
        assert t["function"]["parameters"]["type"] == "object"


# --- full-stack integration (ChatService -> LLM agent -> DB) ---------------

def test_llm_agent_creates_real_card_through_service_app(app, monkeypatch):
    """End-to-end: ChatService drives the LLM agent, which calls create_card
    through the real ToolRegistry + sqlite UnitOfWork, and a real card lands
    in the DB. (ChatService.post_and_respond is what both HTTP and WS use; here
    we route it through the Slice D MultiAgentProvider facade with the scripted
    LLM agent standing in as the planner.)"""
    from crewspace.application.services import ChatService
    from crewspace.config import get_settings
    from crewspace.infrastructure.agents.registry import AgentRegistry, MultiAgentProvider

    client = _ScriptedClient([
        _completion(_msg(None, [_tool_call("c1", "create_card", {"column_id": "col_todo", "title": "LLM real card"})])),
        _completion(_msg("Done — created «LLM real card».", None)),
    ])
    agent = LLMAgent.from_registry(
        build_registry(), api_key="test", model="test-model",
        client_factory=lambda *a, **k: client,
    )
    # Route chat through the facade with this agent as the planner.
    async def _fake_build(settings, uow):
        return MultiAgentProvider({agent.agent_id: agent}, default_agent_id=agent.agent_id)
    monkeypatch.setattr(AgentRegistry, "build", staticmethod(_fake_build))

    async def _run():
        async with app.state.db.uow() as uow:
            svc = ChatService(build_registry(), get_settings())
            msgs = await svc.post_and_respond("chan_general", "user_bilal", "@planner add a card", uow)
            bodies = [m.body for m in msgs]
            assert any("LLM real card" in b for b in bodies), bodies
            board = await uow.boards.get_board("board_main")
            assert board is not None
            all_titles = [c.title for col in board.columns for c in col.cards]
            assert "LLM real card" in all_titles, all_titles

    asyncio.run(_run())
