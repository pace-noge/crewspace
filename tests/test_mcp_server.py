"""M2: MCP Exposure — the Tool Registry served as an MCP server.

Uses the in-process MCP `Client(server)` transport (no subprocess). We assert:
  * the MCP server advertises the same 6 tools as the registry,
  * calling `create_card` through MCP persists a real card in the DB,
  * `find_card` (called through MCP) reads that card back,
  * the `board://` resource returns the board snapshot.
This proves the agent's tools are the canonical, reusable set (PLAN M0/M1/M2).
"""
from __future__ import annotations

import asyncio
import json
import tempfile
from pathlib import Path

import pytest
from mcp.client import Client

from crewspace.config import Settings
from crewspace.infrastructure.db import Database
from crewspace.infrastructure.mcp_server import build_mcp_server


@pytest.fixture
def settings():
    tmp = Path(tempfile.mkdtemp())
    s = Settings(db_path=str(tmp / "mcp.db"))
    db = asyncio.run(Database.create(s))
    asyncio.run(db.close())
    return s


def _text(result) -> str:
    if result.content:
        return result.content[0].text
    return str(result)


def test_mcp_lists_registry_tools(settings):
    server = build_mcp_server(settings)
    out = asyncio.run(_list_tools(server))
    names = {t.name for t in out.tools}
    assert {"create_card", "move_card", "comment_card", "find_card", "list_columns", "post_message"} <= names


def test_mcp_create_and_find_card(settings):
    server = build_mcp_server(settings)
    created, found = asyncio.run(_create_then_find(server))
    assert "MCP card" in created, created
    assert "MCP card" in found, found
    card = json.loads(found)
    assert card["title"] == "MCP card"
    assert card["column_id"] == "col_todo"


def test_mcp_board_resource(settings):
    server = build_mcp_server(settings)
    payload = asyncio.run(_read_board(server))
    assert payload["id"] == "board_main"
    assert any(col["name"] == "To Do" for col in payload["columns"])


async def _list_tools(server):
    async with Client(server) as client:
        return await client.list_tools()


async def _create_then_find(server):
    async with Client(server) as client:
        created = _text(await client.call_tool("create_card", {"column_id": "col_todo", "title": "MCP card"}))
        found = _text(await client.call_tool("find_card", {"board_id": "board_main", "title": "MCP card"}))
        return created, found


async def _read_board(server):
    async with Client(server) as client:
        res = await client.read_resource("board://board_main")
        # ReadResourceResult.contents is a list of TextResourceContents.
        text = res.contents[0].text
        return json.loads(text)
