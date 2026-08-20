"""Infrastructure: real LLM-backed agent (roadmap M1).

Implements the domain `AgentProvider` protocol by driving an
OpenAI-compatible chat-completions call with function calling. The tool
definitions are taken verbatim from the Tool Registry (so the agent and the
MCP surface can never drift from what the app actually does). Tool calls
returned by the model are executed through the `ToolRunner` (never touching
storage directly), and the final assistant message is returned as the reply.

The OpenAI SDK is the only provider binding here; because it speaks the
OpenAI function-calling shape and accepts `base_url`, any OpenAI-compatible
endpoint (OpenRouter, Together, a local llama.cpp server, etc.) works — no
code change beyond env vars.

Designed for testability: the LLM client is injected (`client_factory`), so a
test can hand in a mocked AsyncOpenAI without any network or API key.
"""
from __future__ import annotations

import hashlib
import re
from typing import Any

from ...domain.entities import CardView
from ...domain.identifiers import PLANNER_AGENT_ID
from ...domain.ports import AgentProvider, ToolRunner
from ..agents.stub import HELP_TEXT

# System prompt: keep the agent scoped to tool use + natural chat. We interpolate
# the agent's own name so the same code serves Planner, Coder, Reviewer, etc.
def _system_prompt(name: str) -> str:
    return (
        f"You are {name}, an AI member of a Slack-meets-Trello kanban workspace. "
        "You can act on the board and chat by calling the provided tools. "
        "When the user asks you to do something on the board (create/move a card, "
        "comment, post a message), call the matching tool. Otherwise reply in "
        "plain, friendly chat. Never invent tool-argument ids you were not given; "
        "use find_card/list_columns to resolve titles to ids. Keep replies short.\n\n"
        "Earlier messages in the conversation are supplied as prior turns. Use them "
        "as context: when asked to summarize a thread or conversation, or to extract "
        "action items / decisions / open questions, ground your answer in those "
        "messages rather than asking the user to repeat them.\n\n"
        "Boards: you do NOT need to ask the user for a board id. If a board-related "
        "tool is called without a board_id, the system uses the caller's single "
        "board automatically. If the caller has several boards (e.g. a manager or "
        "admin), call list_boards to see them (name + id) and either pick the right "
        "one or ask the user which board they mean — present the names, not raw ids."
    )


def _provider_tool_name(tool: Any) -> str:
    if tool.provider == "crewspace":
        return tool.name
    safe = re.sub(r"[^A-Za-z0-9_-]", "_", tool.name)
    digest = hashlib.sha256(tool.name.encode()).hexdigest()[:10]
    return f"mcp_{safe[:49]}_{digest}"


def _to_openai_tools(registry_tools: list[Any]) -> list[dict[str, Any]]:
    """Convert registry tools into the OpenAI function-calling schema."""
    return [
        {
            "type": "function",
            "function": {
                "name": _provider_tool_name(t),
                "description": t.description,
                "parameters": t.input_schema,
            },
        }
        for t in registry_tools
    ]


class LLMAgent:
    """AgentProvider backed by an OpenAI-compatible chat model."""

    def __init__(
        self,
        registry_tools: list[Any],
        api_key: str | None = None,
        base_url: str | None = None,
        model: str = "gpt-4o-mini",
        *,
        client_factory=None,
        max_tool_rounds: int = 5,
        agent_id: str = PLANNER_AGENT_ID,
        name: str = "Planner",
        mention: str | None = None,
    ) -> None:
        self._tools = registry_tools
        self._tool_aliases = {
            _provider_tool_name(tool): tool.name for tool in registry_tools
        }
        self._api_key = api_key
        self._base_url = base_url
        self._model = model
        self._client_factory = client_factory
        self._max_tool_rounds = max_tool_rounds
        self.agent_id = agent_id
        self.name = name
        self._mention = (mention or name).strip().lstrip("@").lower()

    # --- construction helpers ----------------------------------------------

    @classmethod
    def from_registry(
        cls,
        registry,
        *,
        api_key: str | None = None,
        base_url: str | None = None,
        model: str = "gpt-4o-mini",
        client_factory=None,
        agent_id: str = PLANNER_AGENT_ID,
        name: str = "Planner",
    ) -> "LLMAgent":
        """Build an LLMAgent from a ToolRegistry (used by build_agent)."""
        return cls(
            registry.list_tools(),
            api_key=api_key,
            base_url=base_url,
            model=model,
            client_factory=client_factory,
            agent_id=agent_id,
            name=name,
        )

    def _make_client(self):
        if self._client_factory is not None:
            return self._client_factory(api_key=self._api_key, base_url=self._base_url)
        from openai import AsyncOpenAI

        return AsyncOpenAI(api_key=self._api_key, base_url=self._base_url)

    # --- protocol ----------------------------------------------------------

    async def on_chat_message(
        self, text: str, runner: ToolRunner, context: list[dict[str, str]] | None = None
    ) -> tuple[str, list[str]]:
        from openai import APIError

        # Only react when *this* agent is mentioned (multi-agent routing).
        if f"@{self._mention}" not in text.lower():
            return (self.agent_id, [])

        client = self._make_client()
        # Prior conversation turns (thread or channel history) come first so the
        # model can summarize / extract action items from what was actually said.
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": _system_prompt(self.name)},
            *[
                {"role": c.get("role", "user"), "name": c.get("name", "user"), "content": c["content"]}
                for c in (context or [])
                if c.get("content")
            ],
            {"role": "user", "content": text},
        ]
        openai_tools = _to_openai_tools(self._tools)

        try:
            for _ in range(self._max_tool_rounds):
                resp = await client.chat.completions.create(
                    model=self._model,
                    messages=messages,
                    tools=openai_tools,
                    tool_choice="auto",
                )
                choice = resp.choices[0].message
                messages.append(
                    {
                        "role": "assistant",
                        "content": choice.content,
                        "tool_calls": choice.tool_calls,
                    }
                )

                # No tool calls -> the model is done talking; return the reply.
                if not choice.tool_calls:
                    return (self.agent_id, [choice.content] if choice.content else [])

                # Execute each requested tool via the registry-bound runner.
                for tc in choice.tool_calls:
                    args = _parse_args(tc.function.arguments)
                    canonical_name = self._tool_aliases.get(
                        tc.function.name, tc.function.name
                    )
                    try:
                        result = await runner.run(canonical_name, **args)
                    except Exception as exc:  # surface tool failures to the model
                        result = {"error": f"{type(exc).__name__}: {exc}"}
                    messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": tc.id,
                            "name": tc.function.name,
                            "content": _stringify(result),
                        }
                    )
            # Hit the round cap: ask the model for a final summary.
            resp = await client.chat.completions.create(
                model=self._model, messages=messages
            )
            content = resp.choices[0].message.content
            return (self.agent_id, [content] if content else [])
        except APIError as exc:
            return (self.agent_id, [f"⚠️ LLM error: {exc}. (Mention `@{self._mention} help` to see what I can do.)"])
        except Exception as exc:  # noqa: BLE001 - don't crash the WS loop
            return (self.agent_id, [f"⚠️ Agent hit an error: {exc}"])

    async def on_card_created(self, card: CardView, runner: ToolRunner) -> None:
        # Optional: a real LLM could summarize/auto-assign here. For now we keep
        # the same friendly note as the stub so the board UX is unchanged.
        await runner.run(
            "comment_card",
            card_id=card.id,
            author_id=self.agent_id,
            body=f"🤖 Noted: «{card.title}». I'll help track this.",
        )


def _parse_args(raw: str | None) -> dict[str, Any]:
    import json

    if not raw:
        return {}
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return {}


def _stringify(value: Any) -> str:
    import json

    try:
        return json.dumps(value, default=str)
    except (TypeError, ValueError):
        return str(value)
