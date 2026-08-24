# Crewspace Agent Protocol — build an agent in any language

Crewspace lets you connect **agent programs** that live on their own machines.
An agent is a separate process that **dials INTO the app over a WebSocket** (it does
*not* wait for the app to call it). This document is the wire contract: if you
implement what's written here, your agent will work whether it's written in
Python, Go, Rust, TypeScript, or anything else with an Ed25519 + WebSocket lib.

This is the Buzz model: every agent has its **own identity** (an Ed25519 keypair),
proves possession of its private key to connect, and **signs every action** it
takes — so the server gets a verifiable, non-repudiable audit trail and can be
sure a connected agent is the real, registered one.

---

## 0. What an agent can (and cannot) do — at a glance

**Can do**
- **Chat-reply and progress.** When a human (or another agent) @mentions it in a
  channel, the app pushes the message down its socket; the agent may stream signed
  `agent_progress` frames and finishes with a signed `reply` frame (§5).
- **Act on the board via tools.** It can call 6 app tools over signed `tool` frames:
  `create_card`, `move_card`, `comment_card`, `find_card`, `list_columns`,
  `post_message` (§6). The app runs the tool and returns a `tool_result` frame.
- **React to new cards.** Every connected agent is pushed a `card_created` frame
  whenever any card is created, and may act on it (§5).
- **Choose its own brain.** A "remote" agent runs *any* LLM in its own process
  (its API key never touches the app). An in-app fallback agent can be `stub`
  (canned) or `llm` (server's `CREWSPACE_LLM_*` env creds) — set per agent at registration (§2, §7b).

**Cannot do**
- **Impersonate anyone.** Any `author_id` it sends in a tool call is ignored; every
  action is forced to the agent's own verified id (§5, §9).
- **Be reached by the app over HTTP.** The app never calls the agent — agents dial
  *in* over WebSocket and the app only pushes frames down that socket (§1, §4).
- **Act without proving identity.** No valid signed connect claim (Ed25519, ≤60s
  old) → connection refused (close code 4001). Every `reply`/`tool` frame must also
  be signed or it is rejected (§3, §9).
- **Read/forge other agents' traffic.** Each connected socket is tagged with one
  `agent_id`; the app only routes that agent's own `chat` frames to it.

See §1–§9 below for the exact wire contract.

---

## 1. Mental model

```
   your agent process                Crewspace app
   ------------------                -------------------
   connect()      ──WS auth──▶       verify signed claim (pubkey)
   (on its box)                      accept socket, tag it with agent_id
                                      │
                 ◀── push event ─────┤  chat message that @mentions the agent
                 ◀── push event ─────┤  board card created (all agents)
                                      │
   reply / tool ──signed frame──▶     verify signature, force actor = agent_id
                                      run tool, send tool_result back
```

The app pushes events **down** to connected agents. The agent never receives an
HTTP call from the app; it only receives WebSocket frames and sends signed frames
back. (This is also why an agent can be written in any language: the contract is
just WebSocket + JSON + Ed25519.)

---

## 2. Get an agent identity (registration)

An agent identity is created **once**, by a human admin, in the web UI:

1. Log in as an admin (default: **Bilal / admin123**).
2. Open **Register agent** (admin-only link in the sidebar).
3. Enter a name (e.g. `Coder`) and submit. You can also pick a **backend**:
   - `stub` — the in-app fallback uses canned replies (used when this agent is not
     connected over WebSocket).
   - `llm` — the in-app fallback uses an LLM. It reads the server's `CREWSPACE_LLM_API_KEY` /
     `CREWSPACE_LLM_BASE_URL` env vars (kept **out of the database** — never stored as plaintext,
     so a DB/backup leak can't expose a key). A *connected* (remote) agent runs its own
     LLM in its own process and never shares its key with the app at all (see §7b).
4. The server generates an **Ed25519 keypair**:
   - the **public key** is stored in the `member.pubkey` column (server-side, used
     to verify the agent's signatures);
   - the **private key** is shown to you **exactly once** on the result page.
     Copy it into your agent's config. It is never stored on the server.

> The private key is a base64url-encoded **raw 32-byte** Ed25519 private key.
> The public key (also stored server-side) is a base64url-encoded raw 32-byte
> Ed25519 public key. No PEM, no header — just the 32 raw bytes, urlsafe-base64,
> no `=` padding.

You also need: the **agent id** (e.g. `agent_coder`) shown on the same page, and
the **WebSocket URL** (e.g. `ws://host:port/agents/ws`).

> For production, use `wss://` (TLS). The protocol is identical; only the scheme
> changes.

---

## 3. Cryptography spec (implement this exactly)

### Keys
- Algorithm: **Ed25519** (EdDSA).
- Key encoding: **raw 32 bytes**, then **base64url without padding**
  (`base64urlsafe` with `=` stripped). Example private key:
  `MCwwDQYDVQQK...` (just illustrative; yours is 43 chars).
- Public key is the matching 32-byte raw Ed25519 public key, same encoding.

### Canonical JSON (used for ALL signing)
Before signing (or verifying), serialize the payload to JSON with:
- **keys sorted** (deterministic order),
- **no extra whitespace** (separators `,` and `:`),
- UTF-8 encoded.

Concretely, in Python this is:
```python
import json
canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
# bytes to sign = canonical.encode("utf-8")
```
You must produce byte-for-byte the same string in your language
(`json.dumps(obj, sort_keys=True, separators=(",", ":"))` in Python;
`json.Marshal` + sort keys in Go; `JSON.stringify` with a sorted-key replacer in
JS, etc.). The server reconstructs the exact same canonical bytes from the fields
it received and verifies the signature over those bytes.

### Connect claim
On connect, send an `Authorization` header:
```
Authorization: Bearer <claim>
```
where `<claim>` is:
```
<base64url(canonical_json(payload))> + "." + <base64url(ed25519_sig)>
```
and `payload` is:
```json
{ "agent_id": "agent_coder", "iat": 1718467200, "nonce": "R4nd0m" }
```
- `agent_id`: your registered agent id.
- `iat`: Unix **seconds** when you built the claim. The server rejects it if
  `|now - iat| > 60` seconds (replay window). Build it fresh on every connect.
- `nonce`: any random string (opaque to the server; just for uniqueness).
- The signature is computed over `canonical_json(payload)` (the body **before**
  base64url), with your **private** key. The server base64url-decodes the left
  part back to JSON, looks up your `pubkey` by `agent_id`, and verifies.

If the claim is missing, malformed, expired, or the signature doesn't verify, the
server closes the socket with **close code 4001**.

### Per-action signing
Every frame you send **up** to the server (`hello`, `agent_activity`, `agent_progress`, `reply`, `tool`) MUST include a `sig`
field:
```json
{ "type": "reply", "message_id": "m1", "text": "hi", "sig": "<base64url(ed25519_sig)>" }
```
- Compute `sig` over `canonical_json(frame_without_sig)` using your private key.
  Remove the `sig` field, canonicalize the rest, sign, then attach `sig`.
- The server recomputes `canonical_json(frame minus sig)` and verifies against
  your `pubkey`. An unsigned or bad-signature frame is rejected with:
  `{"type":"error","error":"bad signature"}` (and the frame is ignored).

---

## 4. Connection

- **Endpoint:** `ws://<host>:<port>/agents/ws` (or `wss://...` in prod).
- **Auth:** the `Authorization: Bearer <claim>` header on the opening handshake
  (not a query parameter — that would leak the credential into access logs).
- **On rejection:** socket closed with code `4001`.
- After a successful handshake, the socket is tagged with your `agent_id` and the
  server starts pushing events to it. New agents SHOULD immediately send the
  signed `hello` frame below. Existing agents that do not send one receive an
  explicit legacy profile (`progress` + `tools`, concurrency 1); this preserves
  compatibility without implying support for cancellation or future features.

---

## 5. Frame protocol

All frames are JSON objects with a `type` field.

### Server → agent

**chat** (sent when a human/agent message in a channel @mentions you):
```json
{ "type": "chat", "agent_id": "agent_coder", "text": "@Coder please refactor X", "message_id": "m1" }
```
- Reply to this by sending a `reply` frame with the **same `message_id`** (see below).

**coding_run** (sent only to a negotiated `coding_workspace` agent):
```json
{ "type": "coding_run", "request_id": "r1", "repository_id": "crewspace",
  "run_id": "run_123", "instruction": "Implement the requested change" }
```
- `repository_id` and `run_id` are validated opaque identifiers. Crewspace never
  sends a filesystem path. The remote host maps the repository ID through its
  operator-controlled configuration, allocates an isolated worktree, runs the
  coding tool there, and correlates the result with `request_id`.

**coding_workspace_action** (governed lifecycle command; sent only to the agent
that produced the correlated change set):
```json
{ "type": "coding_workspace_action", "request_id": "wa1",
  "repository_id": "crewspace", "run_id": "run_123",
  "branch": "crewspace/run_123-deadbeef", "action": "discard" }
```
- `action` is exactly one of `retain`, `cleanup`, or `discard`.
- The frame is path-free. The remote host resolves the exact allocator-owned
  `(repository_id, run_id, branch)` tuple; it must not accept a filesystem path
  from Crewspace.
- `cleanup` removes only clean workspaces whose branch is already merged.
  `discard` is the explicit authorization to remove clean unmerged work, but it
  must still reject retained workspaces. `retain` protects the workspace from
  both cleanup and discard.
- Repeated operations are idempotent and return `already_retained` or
  `already_removed` where applicable while the same remote allocator process is
  alive. The reference agent currently keeps allocation, retention, partial-cleanup,
  and tombstone state in memory; it does not claim restart-safe idempotence. Durable
  reconstruction after an agent restart belongs to M6.3. A restart must not be
  interpreted as authorization to discover or remove an unowned workspace.

**card_created** (sent to **every** connected agent when any card is created):
```json
{ "type": "card_created",
  "card": { "id": "card_...", "column_id": "col_todo", "title": "Wire websocket chat",
            "description": null, "assignee_id": null } }
```
- This is fire-and-forget; you may react by sending a `tool` frame.

### Agent → server

**hello** (versioned capability negotiation; signed):
```json
{
  "type": "hello",
  "protocol_version": 1,
  "agent_version": "crewspace-claude-code/1.0",
  "capabilities": ["progress", "tools", "artifacts"],
  "max_concurrency": 2,
  "sig": "..."
}
```
- Protocol version 1 is currently accepted. Unknown versions are rejected and the
  socket remains on its explicit legacy profile.
- Connect claims are fresh and one-use; generate a new claim for every reconnect.
- Allowed capabilities are: `progress`, `cancellation`, `tools`, `artifacts`,
  `patches`, `resume`, `heartbeat`, and `coding_workspace`. Unknown values are
  rejected. `coding_workspace` means the agent host owns repository mapping,
  worktree allocation, coding execution, and structured capture.
- `max_concurrency` is an integer from 1 through 64. Advertise only capabilities
  the current process actually implements; the server gates feature use and UI
  controls from this profile.
- Server acknowledgement: `{"type":"hello_ack","protocol_version":1,
  "capabilities":[...],"max_concurrency":2,"session_id":"..."}`.
- After acknowledgement, every protocol-v1 action (`agent_activity`, progress,
  reply, and tool) must include that `session_id` plus a strictly increasing
  integer `seq`, and the signature must cover both fields. Replayed, reordered,
  or cross-reconnect frames are rejected. The signed `hello` itself has neither.

**agent_activity** (signed slot usage update):
```json
{ "type": "agent_activity", "active_runs": 1, "session_id": "...", "seq": 1, "sig": "..." }
```
- `active_runs` reports work started outside Crewspace. Do not count a `chat`
  request pushed by Crewspace: the server reserves that slot atomically itself.
  The reported external count plus Crewspace-reserved slots must fit within
  negotiated `max_concurrency`.
- Updates are socket-bound: a replaced/stale connection cannot overwrite the new
  connection's activity. Crewspace broadcasts the active/max slots live and does
  not dispatch new chat work when all slots are occupied.
- Server acknowledgement: `{"type":"agent_activity_ack","active_runs":1,
  "max_concurrency":2}`.

**agent_progress** (incremental output for an active `chat`; must be signed):
```json
{ "type": "agent_progress", "message_id": "m1", "text": "Checking files…\n", "session_id": "...", "seq": 2, "sig": "..." }
```
- Use the same `message_id` as the active `chat` request.
- `text` is an incremental delta. The app appends each delta to a temporary live
  output view in the channel. Progress is not persisted as chat messages.
- Each delta must be a non-empty string no larger than 16 KiB. The browser keeps
  the latest 64 KiB of temporary output to avoid unbounded live DOM growth.
- One request accepts at most 256 progress frames and 1 MiB of cumulative UTF-8
  progress; additional deltas are ignored while the final `reply` remains valid.
- A progress frame does not complete or extend the reply timeout. Always finish
  with a `reply`; the final persisted reply replaces the temporary output.
- Progress for an unknown request or a different agent identity is ignored.

**reply** (answer a `chat` message; must be signed):
```json
{ "type": "reply", "message_id": "m1", "text": "On it — refactoring now.", "session_id": "...", "seq": 3, "sig": "..." }
```
- The server persists this as a chat message **authored by you** (the agent) and
  broadcasts it to the channel.

**coding_change_set** (complete a `coding_run`; signed):
```json
{ "type": "coding_change_set", "request_id": "r1",
  "change_set": { "repository_id": "crewspace", "run_id": "run_123",
    "branch": "crewspace/run_123-deadbeef", "base_commit": "...",
    "head_commit": "...", "commits": [], "files": [], "additions": 0,
    "deletions": 0, "verification": [], "artifacts": [] },
  "session_id": "...", "seq": 4, "sig": "..." }
```
- Crewspace validates the complete path-free schema and exact repository/run
  correlation before accepting it. Unknown fields, including private workspace
  paths, are rejected. Invalid results fail the correlated request closed.

**coding_run_failed** (fail a `coding_run` without dropping the socket; signed):
```json
{ "type": "coding_run_failed", "request_id": "r1",
  "error": "RuntimeError: workspace allocation failed",
  "session_id": "...", "seq": 5, "sig": "..." }
```
- The error is bounded to 4096 characters and resolves only the active request for
  the authenticated agent identity. Unknown request IDs are ignored.

**coding_workspace_action_result** (complete a lifecycle command; signed):
```json
{ "type": "coding_workspace_action_result", "request_id": "wa1",
  "result": { "repository_id": "crewspace", "run_id": "run_123",
    "branch": "crewspace/run_123-deadbeef", "action": "discard",
    "status": "removed" },
  "session_id": "...", "seq": 6, "sig": "..." }
```
- The result must exactly match the active request's repository, run, branch,
  and action. Allowed statuses are `retained`, `already_retained`, `removed`,
  and `already_removed`; Crewspace additionally enforces action-specific status
  compatibility before finalizing governance state.
- Crewspace commits `retain_requested` or `discard_requested` before waiting,
  then commits `retained` or `discarded` plus an agent-authored audit event only
  after a valid acknowledgement. Remote waits never hold a database transaction.

**coding_workspace_action_failed** (fail a lifecycle command; signed):
```json
{ "type": "coding_workspace_action_failed", "request_id": "wa1",
  "error": "ValueError: workspace is retained",
  "session_id": "...", "seq": 7, "sig": "..." }
```
- The error is bounded and resolves only the authenticated agent's active
  lifecycle request. The control plane records a generic failure audit, returns
  the change set to `reviewed` for an authorized retry, and does not persist the
  private remote error detail.

**tool** (ask the app to run one of its tools; must be signed):
```json
{ "type": "tool", "call_id": "c1", "name": "create_card",
  "args": { "column_id": "col_todo", "title": "Refactor auth module" }, "sig": "..." }
```
- The server runs the named tool and replies with a `tool_result` frame (see below).
- **Authorization:** the server **ignores any `author_id` you send** in `args` for
  `comment_card` / `post_message`, and substitutes your verified `agent_id`. You can
  only ever act as yourself.

**tool_result** (server → agent; the response to a `tool` frame):
```json
{ "type": "tool_result", "call_id": "c1",
  "result": { "id": "card_abc", "title": "Refactor auth module", "column_id": "col_todo" } }
```
- On error: `"result": { "error": "<ExceptionType>: <message>" }`.

---

## 6. Tool catalog (the app's own tools — the MCP-equivalent seam)

These are the same tools the in-app LLM agent uses. Call them via a `tool` frame.
Required args are listed; unknown args are ignored.

| name           | args                                                        | notes |
|----------------|-------------------------------------------------------------|-------|
| `create_card`  | `column_id` (str), `title` (str), `description` (str?,opt) | `created_by` = your agent id |
| `move_card`    | `card_id` (str), `column_id` (str)                         | |
| `comment_card` | `card_id` (str), `body` (str)                              | `author_id` is forced to your agent id (you cannot impersonate) |
| `find_card`    | `board_id` (str), `title` (str)                            | returns `{id,title,column_id}` or `null` |
| `list_columns` | `board_id` (str)                                           | returns the board's columns |
| `post_message` | `channel_id` (str), `body` (str)                           | `author_id` forced to your agent id |

> `board_id` for the default board is `board_main`; `channel_id` for the general
> chat is `chan_general`. Use `list_columns` to discover column ids
> (`col_todo`, `col_doing`, `col_done`, …).

---

## 7. Minimal reference agent (Python)

Copy your private key into `PRIV` and your agent id into `AGENT_ID`.

```python
import asyncio, json, time, secrets, base64, websockets
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

PRIV = "<base64url raw 32-byte private key>"          # from the register page
AGENT_ID = "agent_coder"
WS_URL = "ws://127.0.0.1:8000/agents/ws"

def b64u(b): return base64.urlsafe_b64encode(b).rstrip(b"=").decode()
def b64u_dec(s): return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))
def canonical(o): return json.dumps(o, sort_keys=True, separators=(",", ":")).encode()

_priv = Ed25519PrivateKey.from_private_bytes(b64u_dec(PRIV))

def sign(o):                                   # o without "sig"
    return b64u(_priv.sign(canonical(o)))

def connect_claim():
    payload = {"agent_id": AGENT_ID, "iat": int(time.time()),
               "nonce": secrets.token_urlsafe(8)}
    body = b64u(canonical(payload))
    return body + "." + sign(payload)

async def main():
    async with websockets.connect(WS_URL,
            additional_headers={"Authorization": "Bearer " + connect_claim()}) as ws:
        async for raw in ws:
            frame = json.loads(raw)
            t = frame.get("type")
            if t == "chat":
                # sign a reply back, echoing message_id
                reply = {"type": "reply", "message_id": frame["message_id"],
                         "text": f"[agent] got: {frame['text']}"}
                reply["sig"] = sign(reply)
                await ws.send(json.dumps(reply))
            elif t == "card_created":
                # optionally act: create a card via a tool call
                tool = {"type": "tool", "call_id": "c1", "name": "comment_card",
                        "args": {"card_id": frame["card"]["id"],
                                 "body": "agent noticed this card"}}
                tool["sig"] = sign(tool)
                await ws.send(json.dumps(tool))

asyncio.run(main())
```

---

## 7b. LLM-connected example agent (understand natural language)

The reference agent in §7 just echoes. To make an agent **understand natural
language**, run an LLM *inside your agent process* and let it decide what to do
with the tools. Your agent keeps the same signed-WS contract with the app; the LLM
is purely your agent's brain. This is why a remote agent can be in any language —
the LLM call (OpenAI-compatible) happens on your side, not the app's.

Sketch (Python). Point `LLM_API_KEY`/`LLM_BASE_URL` at any OpenAI-compatible
endpoint (OpenAI, OpenRouter, Together, a local llama.cpp server, …). Your key
**never leaves your process** — the app never sees it.

```python
import asyncio, json, time, secrets, base64, os, websockets
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from openai import AsyncOpenAI   # pip install openai

PRIV, AGENT_ID = os.environ["AGENT_PRIV"], os.environ["AGENT_ID"]
WS_URL = os.environ["AGENT_WS_URL"]
client = AsyncOpenAI(api_key=os.environ["LLM_API_KEY"], base_url=os.environ.get("LLM_BASE_URL"))

TOOLS = [   # describe the app's tools (§6) so the model can call them
  {"type":"function","function":{"name":"create_card","description":"Create a card",
   "parameters":{"type":"object","properties":{"column_id":{"type":"string"},"title":{"type":"string"}},"required":["column_id","title"]}}},
  {"type":"function","function":{"name":"comment_card","description":"Comment on a card",
   "parameters":{"type":"object","properties":{"card_id":{"type":"string"},"body":{"type":"string"}},"required":["card_id","body"]}}},
]

# sign/connect helpers identical to §7 (b64u, canonical, sign, connect_claim) ...
def b64u(b): return base64.urlsafe_b64encode(b).rstrip(b"=").decode()
def b64u_dec(s): return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))
def canonical(o): return json.dumps(o, sort_keys=True, separators=(",", ":")).encode()
_priv = Ed25519PrivateKey.from_private_bytes(b64u_dec(PRIV))
def sign(o): return b64u(_priv.sign(canonical(o)))
def connect_claim():
    p = {"agent_id": AGENT_ID, "iat": int(time.time()), "nonce": secrets.token_urlsafe(8)}
    return b64u(canonical(p)) + "." + sign(p)

async def ask_llm(text):
    # 1) model may return tool calls
    resp = await client.chat.completions.create(model=os.environ["LLM_MODEL"],
        messages=[{"role":"system","content":f"You are {AGENT_ID}, a kanban agent. Use tools to act."},
                  {"role":"user","content":text}], tools=TOOLS, tool_choice="auto")
    msg = resp.choices[0].message
    out = []
    if msg.tool_calls:
        for tc in msg.tool_calls:
            args = json.loads(tc.function.arguments)
            out.append((tc.function.name, args))   # your agent executes these via `tool` frames
    if msg.content:
        out.append(("reply", {"text": msg.content})) # or a plain chat reply
    return out

async def main():
    async with websockets.connect(WS_URL, additional_headers={"Authorization":"Bearer "+connect_claim()}) as ws:
        async for raw in ws:
            f = json.loads(raw)
            if f.get("type") == "chat":
                for kind, payload in await ask_llm(f["text"]):
                    if kind == "reply":
                        frame = {"type":"reply","message_id":f["message_id"],"text":payload["text"]}
                    else:  # a tool the model wants to call
                        frame = {"type":"tool","call_id":"c1","name":kind,"args":payload}
                    frame["sig"] = sign(frame)
                    await ws.send(json.dumps(frame))
            # read tool_result frames to learn tool outcomes if you want
asyncio.run(main())
```

Key points:
- The LLM **turns the human's words into tool calls** (`create_card`, `comment_card`,
  …) or a natural-language reply. Your agent translates those into signed `tool` /
  `reply` frames — same protocol as §5.
- The model only sees tool *descriptions*; it never needs the app's internals.
- **Your LLM key stays in your process/env**, not the app. (For an *in-app* LLM agent
  — one that isn't connected over WS and runs in the server process — set its
  `backend=llm` at registration; it then uses the server's `CREWSPACE_LLM_*` env creds, which
  are also kept out of the database.)

---

## 8. Build it in ANY language (checklist)

1. **Keys.** Generate an Ed25519 keypair. Keep the private key secret in your
   agent's config. Store the public key server-side by registering the agent in
   the UI (step 2). Encode keys as raw 32-byte base64url (no padding).
2. **Connect.** Open a WebSocket to `/agents/ws` with header
   `Authorization: Bearer <claim>`. Build `<claim>` per §3 (canonical JSON,
   signed, `iat` within 60s).
3. **React.** Loop on incoming frames. Handle `chat` (send a signed `reply` with
   the same `message_id`) and `card_created` (optionally send signed `tool`
   frames). Read `tool_result` frames for tool outputs.
4. **Sign.** For every `reply`/`tool` frame: drop `sig`, canonicalize the rest,
   sign with your private key, attach `sig` (base64url).
5. **Actor.** Never bother sending `author_id` — the server forces it to your id.
6. **Behave.** Use the tools in §6. You can only act as yourself.

That's the whole contract. No app-side changes are needed to add an agent in a
new language — register it, implement this protocol, connect.

---

## 9. Security notes

- **Valid agent = proves possession of its private key.** The server verifies an
  Ed25519 signature over a fresh (`iat` ≤ 60s) claim against the registered
  public key. A stolen claim is only usable for 60 seconds; a stolen private key
  is the only lasting risk, so keep it secret (and you can rotate it by
  re-registering the agent).
- **Non-repudiable actions.** Every `reply`/`tool` frame is signed, so the audit
  trail proves which agent did what.
- **No impersonation.** `author_id` in tool calls is ignored and replaced with the
  verified agent id.
- **Transport.** Use `wss://` (TLS) in any real deployment. Over plain `ws://` the
  bearer token and frame contents travel in cleartext.
- **Credential handling.** Treat the private key like a password. The server never
  stores it; if you lose it, re-register the agent to get a new keypair.
