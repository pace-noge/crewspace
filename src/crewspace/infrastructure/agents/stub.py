"""Infrastructure: local stub agent (regex-driven canned agent).

Implements the domain ``AgentProvider`` protocol using only tools. Unlike the
original hard-coded planner, it now takes an ``agent_id`` and ``mention`` so the
same class backs ANY registered local agent (planner, coder, reviewer, …). The
mention name is how the ``MultiAgentProvider`` facade routes chat to it.
"""
from __future__ import annotations

import re

from ...domain.entities import CardView
from ...domain.identifiers import COLUMN_IDS
from ...domain.ports import AgentProvider, ToolRunner

HELP_TEXT = (
    "I can: `new card \"Title\" in Todo|Doing|Done`, "
    "`move \"Title\" to Todo|Doing|Done`, or just chat."
)


class StubAgent:
    """Regex-driven canned agent. Implements AgentProvider using only tools."""

    def __init__(self, agent_id: str, name: str, mention: str | None = None) -> None:
        self.agent_id = agent_id
        self.name = name
        # The token that routes chat to this agent, e.g. "@planner" -> "planner".
        self._mention = (mention or name).strip().lstrip("@").lower()

    def hears(self, text: str) -> bool:
        return bool(re.search(rf"@{re.escape(self._mention)}\b", text, re.I))

    async def on_chat_message(
        self, text: str, runner: ToolRunner, context: list[dict[str, str]] | None = None
    ) -> tuple[str, list[str]]:
        t = text.strip()
        if not self.hears(t):
            return (self.agent_id, [])
        if re.search(r"\bhelp\b", t, re.I):
            return (self.agent_id, [HELP_TEXT])
        m = re.search(r'new card\s+"([^"]+)"\s+in\s+(todo|doing|done)', t, re.I)
        if m:
            res = await runner.run("create_card", column_id=COLUMN_IDS[m.group(2).lower()], title=m.group(1))
            return (self.agent_id, [f"Created card «{res['title']}» in {m.group(2).title()}."])
        m = re.search(r'move\s+"([^"]+)"\s+to\s+(todo|doing|done)', t, re.I)
        if m:
            card = await runner.run("find_card", board_id=None, title=m.group(1))
            if not card:
                return (self.agent_id, [f"Couldn't find a card named «{m.group(1)}»."])
            moved = await runner.run("move_card", card_id=card["id"], column_id=COLUMN_IDS[m.group(2).lower()])
            return (self.agent_id, [f"Moved «{moved['title']}» to {m.group(2).title()}."])
        return (self.agent_id, ["Got it. Mention `@" + self._mention + " help` to see what I can do."])

    async def on_card_created(self, card: CardView, runner: ToolRunner) -> None:
        await runner.run(
            "comment_card",
            card_id=card.id,
            author_id=self.agent_id,
            body=f"🤖 {self.name}: noted «{card.title}». I'll help track this.",
        )
