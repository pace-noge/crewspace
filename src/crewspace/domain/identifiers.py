"""Stable identifiers.

Seeded entities use fixed ids so templates, the agent, and tests can reference
them reliably. Centralized here so they aren't scattered as string literals.
"""
from __future__ import annotations

DEFAULT_WORKSPACE_ID = "ws_default"
DEFAULT_BOARD_ID = "board_main"
DEFAULT_CHANNEL_ID = "chan_general"
PLANNER_AGENT_ID = "agent_planner"
HUMAN_USER_ID = "user_bilal"

# Lowercased column name -> seeded column id (used by agent commands).
COLUMN_IDS: dict[str, str] = {
    "todo": "col_todo",
    "doing": "col_doing",
    "done": "col_done",
}
