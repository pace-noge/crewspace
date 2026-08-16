"""
LLM-connected reference agent (clone-and-run).

This is a complete, runnable agent that connects to Crewspace over the
signed WebSocket protocol and uses its OWN LLM (OpenAI-compatible) to understand
natural language and decide which board tools to call. Your LLM API key lives
ONLY in this process's environment — the app never sees it.

It is the canonical "remote agent" from docs/AGENT_PROTOCOL.md §7b. The protocol
contract (frames, signing, connect claim) is implemented from scratch here so you
can port it to any language.

Run:
    pip install "openai>=1.0" websockets
    export AGENT_PRIV="<base64url raw 32-byte private key from the register page>"
    export AGENT_ID="agent_coder"
    export AGENT_WS_URL="ws://127.0.0.1:8000/agents/ws"
    export LLM_API_KEY="sk-..."
    export LLM_BASE_URL="https://api.openai.com/v1"   # or any OpenAI-compatible URL
    export LLM_MODEL="gpt-4o-mini"
    python llm_agent.py
"""
from __future__ import annotations

import asyncio
import base64
import json
import os
import secrets
import time

import websockets
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from openai import AsyncOpenAI


# --------------------------------------------------------------------------
# Protocol helpers (mirror docs/AGENT_PROTOCOL.md §3 exactly)
# --------------------------------------------------------------------------
def _b64u(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def _b64u_dec(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def _canonical(obj: dict) -> bytes:
    # Deterministic JSON: sorted keys, compact separators. Used for ALL signatures.
    return json.dumps(obj, sort_keys=True, separators=(",", ":")).encode("utf-8")


class Signer:
    def __init__(self, priv_b64u: str) -> None:
        self._priv = Ed25519PrivateKey.from_private_bytes(_b64u_dec(priv_b64u))

    def sign(self, obj: dict) -> str:
        """Ed25519-sign canonical(obj); return base64url signature."""
        return _b64u(self._priv.sign(_canonical(obj)))

    def connect_claim(self, agent_id: str) -> str:
        """Build the signed connect token: base64url(json).base64url(sig)."""
        payload = {"agent_id": agent_id, "iat": int(time.time()), "nonce": secrets.token_urlsafe(8)}
        return _b64u(_canonical(payload)) + "." + self.sign(payload)

    def sign_frame(self, frame: dict) -> dict:
        """Return a copy of the frame with a `sig` field attached (over frame minus sig)."""
        f = dict(frame)
        f["sig"] = self.sign({k: v for k, v in frame.items() if k != "sig"})
        return f


# --------------------------------------------------------------------------
# Tool catalog (described to the LLM; must match docs/AGENT_PROTOCOL.md §6)
# --------------------------------------------------------------------------
TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "create_card",
            "description": "Create a card in a board column.",
            "parameters": {
                "type": "object",
                "properties": {
                    "column_id": {"type": "string", "description": "col_todo|col_doing|col_done"},
                    "title": {"type": "string"},
                    "description": {"type": "string"},
                },
                "required": ["column_id", "title"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "move_card",
            "description": "Move a card to another column.",
            "parameters": {
                "type": "object",
                "properties": {
                    "card_id": {"type": "string"},
                    "column_id": {"type": "string"},
                },
                "required": ["card_id", "column_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "comment_card",
            "description": "Add a comment to a card.",
            "parameters": {
                "type": "object",
                "properties": {
                    "card_id": {"type": "string"},
                    "body": {"type": "string"},
                },
                "required": ["card_id", "body"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "find_card",
            "description": "Find a card by title within a board; returns its id.",
            "parameters": {
                "type": "object",
                "properties": {
                    "board_id": {"type": "string"},
                    "title": {"type": "string"},
                },
                "required": ["board_id", "title"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_columns",
            "description": "List a board's columns (id + name).",
            "parameters": {
                "type": "object",
                "properties": {"board_id": {"type": "string"}},
                "required": ["board_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "post_message",
            "description": "Post a chat message to a channel.",
            "parameters": {
                "type": "object",
                "properties": {
                    "channel_id": {"type": "string"},
                    "body": {"type": "string"},
                },
                "required": ["channel_id", "body"],
            },
        },
    },
]


# --------------------------------------------------------------------------
# Agent loop
# --------------------------------------------------------------------------
async def main() -> None:
    agent_id = os.environ["AGENT_ID"]
    ws_url = os.environ["AGENT_WS_URL"]
    signer = Signer(os.environ["AGENT_PRIV"])

    client = AsyncOpenAI(
        api_key=os.environ["LLM_API_KEY"],
        base_url=os.environ.get("LLM_BASE_URL"),  # None -> default OpenAI
    )
    model = os.environ.get("LLM_MODEL", "gpt-4o-mini")

    async with websockets.connect(
        ws_url, additional_headers={"Authorization": "Bearer " + signer.connect_claim(agent_id)}
    ) as ws:
        print(f"[agent] connected as {agent_id}")
        async for raw in ws:
            frame = json.loads(raw)
            ftype = frame.get("type")

            if ftype == "chat":
                # The app pushed a chat message that @mentioned this agent.
                text = frame["text"]
                message_id = frame["message_id"]
                actions = await _decide(client, model, text)  # -> list of (name, args) or ("reply", {text})
                if not actions:
                    reply = signer.sign_frame(
                        {"type": "reply", "message_id": message_id, "text": "OK, noted."}
                    )
                    await ws.send(json.dumps(reply))
                for name, args in actions:
                    if name == "reply":
                        out = signer.sign_frame(
                            {"type": "reply", "message_id": message_id, "text": args["text"]}
                        )
                    else:
                        out = signer.sign_frame(
                            {"type": "tool", "call_id": "c1", "name": name, "args": args}
                        )
                    await ws.send(json.dumps(out))

            elif ftype == "card_created":
                # Fire-and-forget: a card was created. Optionally act on it.
                card = frame["card"]
                actions = await _decide(
                    client, model, f"A new card was created: '{card['title']}'. Want to comment?"
                )
                for name, args in actions:
                    if name != "reply":
                        out = signer.sign_frame(
                            {"type": "tool", "call_id": "c1", "name": name, "args": args}
                        )
                        await ws.send(json.dumps(out))

            elif ftype == "tool_result":
                # Result of a tool we requested (if we want to react to it).
                pass


async def _decide(client: AsyncOpenAI, model: str, text: str) -> list[tuple[str, dict]]:
    """Ask the LLM to turn `text` into tool calls or a reply.

    Returns a list of (tool_name, args) pairs, or [("reply", {"text": ...})].
    """
    resp = await client.chat.completions.create(
        model=model,
        messages=[
            {
                "role": "system",
                "content": (
                    "You are a kanban agent. Use the provided tools to act on the board "
                    "when the user asks for something concrete (create/move a card, comment, "
                    "post a message). Otherwise reply in plain friendly chat. Never invent "
                    "ids you were not given; use find_card/list_columns to resolve titles."
                ),
            },
            {"role": "user", "content": text},
        ],
        tools=TOOLS,  # type: ignore[arg-type]
        tool_choice="auto",
    )
    msg = resp.choices[0].message
    out: list[tuple[str, dict]] = []
    tool_calls = getattr(msg, "tool_calls", None)
    if tool_calls:
        for tc in tool_calls:
            try:
                args = json.loads(tc.function.arguments or "{}")
            except json.JSONDecodeError:
                args = {}
            out.append((tc.function.name, args))
    if msg.content:
        out.append(("reply", {"text": msg.content}))
    return out


if __name__ == "__main__":
    required = ["AGENT_ID", "AGENT_PRIV", "AGENT_WS_URL", "LLM_API_KEY"]
    missing = [v for v in required if not os.environ.get(v)]
    if missing:
        raise SystemExit(f"Missing env vars: {', '.join(missing)}")
    asyncio.run(main())
