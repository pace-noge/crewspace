"""MCP Exposure (roadmap M2).

Wraps the canonical Tool Registry as an MCP server so an *external* agent
(Claude Desktop, another agent, a script) can discover and call your board/
chat as if it owned those tools. The tool definitions are identical to the
ones the in-app LLM agent uses -- there is no second copy; they come from
`build_registry()`.

The MCP server is a standalone process (separate from the web app), so it
opens its own `Database` handle via the same `Database.create(settings)` seam
the app uses. That handle is created once per server lifetime (via the MCP
`lifespan`) and every tool/resource call runs inside a UnitOfWork, committing
on success and rolling back on error -- exactly the storage-agnostic path the
web app and agent already use.

Resources (read-only projections):
  * board://{board_id}      -> the board's columns + cards (JSON)
  * channel://{channel_id}  -> recent channel messages (JSON)

Run it:
  uv run crewspace-mcp                 # stdio (for Claude Desktop etc.)
  uv run crewspace-mcp --transport sse --port 9000
"""
from __future__ import annotations

import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from mcp.server import MCPServer

from ..application.tools import ToolRegistry, build_registry
from ..config import Settings, get_settings
from ..dto.mappers import to_board, to_message
from ..domain.identifiers import DEFAULT_BOARD_ID, DEFAULT_CHANNEL_ID
from ..domain.ports import UnitOfWork
from ..infrastructure.db import Database


@asynccontextmanager
async def _lifespan(server: MCPServer) -> AsyncIterator[dict[str, Any]]:
    """Open one DB handle for the server's lifetime; close it on shutdown."""
    settings: Settings = server._settings  # type: ignore[attr-defined]
    db = await Database.create(settings)
    server._db = db  # type: ignore[attr-defined]
    try:
        yield {"db": db}
    finally:
        await db.close()


def _serialize(value: Any) -> str:
    """MCP tool results are returned as text content; we JSON-encode dicts."""
    if isinstance(value, (dict, list)):
        return json.dumps(value, default=str)
    return str(value)


def build_mcp_server(settings: Settings | None = None) -> MCPServer:
    """Construct the MCP server, wiring the registry tools + resources.

    The server holds an open DB for its lifetime (via `lifespan`); each tool/
    resource handler opens a UnitOfWork against it.
    """
    settings = settings or get_settings()
    registry: ToolRegistry = build_registry()

    server = MCPServer(
        name="crewspace",
        title="Crewspace",
        description="Shared human-agent workspace, exposed as MCP tools.",
        version="0.3.0",
        lifespan=_lifespan,
    )
    server._settings = settings  # type: ignore[attr-defined]

    # --- tools: one MCP tool per registry tool ----------------------------
    # The MCP framework derives each tool's *call* schema from the handler's
    # function signature, so we build a wrapper with explicit named parameters
    # taken from the registry tool's JSON Schema (a bare `**args` signature
    # confuses the arg parser). The body still routes through the registry.
    def _build_handler(tool_name: str, schema: dict[str, Any]):
        props: dict[str, Any] = schema.get("properties", {})
        required: set[str] = set(schema.get("required", []))

        # Build a signature: each schema property becomes an explicit param,
        # optional ones default to None so the LLM/agent can omit them.
        param_decls = []
        # Python requires parameters without defaults before parameters with
        # defaults. JSON Schema property order is not semantic, so sort the
        # required names first before generating the function signature.
        arg_names = [
            pname for pname in props if pname in required
        ] + [pname for pname in props if pname not in required]
        for pname in arg_names:
            default = " = None" if pname not in required else ""
            param_decls.append(f"{pname}: str{default}")
        sig = ", ".join(param_decls)

        src = (
            f"async def _h({sig}) -> str:\n"
            f"    _kwargs: dict[str, object] = {{}}\n"
        )
        # Map each parameter name to its runtime value inside the handler.
        for n in arg_names:
            src += f"    _kwargs[{n!r}] = {n}\n"
        src += (
            f"    _kwargs = {{k: v for k, v in _kwargs.items() if v is not None}}\n"
            f"    db = _server._db\n"
            f"    async with db.uow() as _uow:\n"
            f"        _runner = _registry.bind(_uow)\n"
            f"        _result = await _runner.run(_tool_name, **_kwargs)\n"
            f"    return _serialize(_result or {{}})\n"
        )
        namespace: dict[str, Any] = {
            "_server": server,
            "_registry": registry,
            "_tool_name": tool_name,
            "_serialize": _serialize,
        }
        exec(src, namespace)
        return namespace["_h"]

    for tool in registry.list_tools():
        handler = _build_handler(tool.name, tool.input_schema)
        server.add_tool(handler, name=tool.name, description=tool.description)
        # Override the type-hint-derived schema with the registry's richer
        # (description-bearing) JSON Schema -- the source of truth.
        server._tool_manager._tools[tool.name].parameters = tool.input_schema  # type: ignore[attr-defined]

    # --- resources --------------------------------------------------------
    @server.resource(f"board://{{board_id}}", name="board", description="Board snapshot: columns + cards")
    async def board_resource(board_id: str) -> str:
        db = server._db  # type: ignore[attr-defined]
        async with db.uow() as uow:
            view = await uow.boards.get_board(board_id)
        return _serialize(to_board(view).model_dump(mode="json") if view else {"error": "board not found"})

    @server.resource(f"channel://{{channel_id}}", name="channel", description="Recent channel messages")
    async def channel_resource(channel_id: str) -> str:
        db = server._db  # type: ignore[attr-defined]
        async with db.uow() as uow:
            msgs = await uow.chat.list_messages(channel_id)
        payload = [to_message(m).model_dump(mode="json") for m in msgs]
        return _serialize(payload)

    server._default_board_id = DEFAULT_BOARD_ID  # type: ignore[attr-defined]
    server._default_channel_id = DEFAULT_CHANNEL_ID  # type: ignore[attr-defined]
    return server


def main() -> None:
    """Console entrypoint (`crewspace-mcp`)."""
    import argparse

    parser = argparse.ArgumentParser(prog="crewspace-mcp", description="MCP server for Crewspace")
    parser.add_argument("--transport", choices=["stdio", "sse", "streamable-http"], default="stdio")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=9000)
    args = parser.parse_args()

    server = build_mcp_server()
    server.run(transport=args.transport, host=args.host, port=args.port)


if __name__ == "__main__":
    main()
