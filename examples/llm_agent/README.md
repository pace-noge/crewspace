# LLM-connected reference agent

A complete, runnable **remote agent** for Crewspace. It connects to the app
over the signed WebSocket protocol and uses its **own** OpenAI-compatible LLM to
understand natural language and decide which board tools to call. Your LLM API key
lives only in this process's environment — the app never sees it.

This is the canonical "remote agent" described in
`docs/AGENT_PROTOCOL.md` §7b, as a clone-and-run file. The same protocol lets you
build an equivalent agent in Go, Rust, TypeScript, etc.

## Prerequisites

- Python 3.12+
- An Crewspace server running (the app)
- An **agent identity**: log into the app as an admin, open *Register agent*, and
  copy the **private key** it shows you once. The agent id is also shown there
  (e.g. `agent_coder`).

## Install

```bash
pip install "openai>=1.0" websockets
```

## Configure (environment variables)

```bash
export AGENT_PRIV="<base64url raw 32-byte private key from the register page>"
export AGENT_ID="agent_coder"
export AGENT_WS_URL="ws://127.0.0.1:8000/agents/ws"   # wss:// in production
export LLM_API_KEY="sk-..."                             # your key, never sent to the app
export LLM_BASE_URL="https://api.openai.com/v1"         # any OpenAI-compatible URL
export LLM_MODEL="gpt-4o-mini"
```

## Run

```bash
python llm_agent.py
```

Then in the app's chat, mention the agent (e.g. `@coder create a card "Write the
API docs" in To Do`) and it will use the LLM to call the board tools on your behalf.
The agent signs every action, so the audit trail records it as `agent_coder`.

## What it does

- Builds the signed connect claim (`Authorization: Bearer <claim>`) per
  `docs/AGENT_PROTOCOL.md` §3.
- Receives `chat` frames the app pushes when someone @mentions it, and `card_created`
  frames for every new card.
- Asks its LLM to turn the message into tool calls (`create_card`, `comment_card`, …)
  or a natural-language reply.
- Sends signed `reply` / `tool` frames back; the app verifies the signature, runs
  the tool, and returns `tool_result`.

## Porting to another language

The protocol is just WebSocket + JSON + Ed25519. Implement these from the spec:
1. Ed25519 keypair; raw 32-byte keys in base64url-without-padding.
2. Canonical JSON (`sort_keys`, compact separators) for signing.
3. Connect claim + per-frame `sig` as documented in §3 and §5.
4. Handle `chat` / `card_created`; send signed `reply` / `tool`; read `tool_result`.

You can use any LLM client on your side — the app only cares about the signed frames.
