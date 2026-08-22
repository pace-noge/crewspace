"""Behavioral tests — chat + board + the agent, end to end.

Run: uv run pytest -q
"""
from __future__ import annotations

import re

from starlette.testclient import TestClient

from crewspace.application.services import agent_routable_text
from crewspace.main import create_app


def test_lifespan_closes_an_injected_database_once():
    class InjectedDatabase:
        def __init__(self):
            self.close_calls = 0

        async def close(self):
            self.close_calls += 1

    database = InjectedDatabase()
    app = create_app()
    app.state.db = database
    app.state.start_schedulers = False

    with TestClient(app):
        pass

    assert database.close_calls == 1
    assert app.state.db_closed_by_lifespan is True

def _create_card(client: TestClient, title: str, column: str = "col_todo") -> str:
    # Create through the real HTTP endpoint (single event loop) and parse the
    # new card id out of the returned HTML fragment.
    r = client.post("/boards/board_main/cards", data={"column_id": column, "title": title})
    assert r.status_code == 200
    m = re.search(r'id="card-([0-9a-f]+)"', r.text)
    assert m, f"no card id in response: {r.text[:200]}"
    return m.group(1)


# --- board -----------------------------------------------------------------


def test_board_page_loads(client):
    r = client.get("/board/board_main")
    assert r.status_code == 200
    assert "Roadmap" in r.text  # board name
    assert "Draft launch announcement" in r.text  # a seeded card


def test_create_card_adds_to_board(client):
    r = client.post(
        "/boards/board_main/cards",
        data={"column_id": "col_todo", "title": "Write the README"},
    )
    assert r.status_code == 200
    assert "Write the README" in r.text
    # agent dropped a note on the card via on_card_created
    assert "noted" in r.text.lower()
    # and it appears on the board page
    board = client.get("/board/board_main").text
    assert "Write the README" in board


def test_move_card_endpoint(client):
    card_id = _create_card(client, "Move me")
    r = client.post(f"/cards/{card_id}/move", data={"column_id": "col_done"})
    assert r.status_code == 200
    # the move now returns a full board fragment (swapped into #board-wrap)
    assert 'class="board"' in r.text
    # the moved card is present and rendered inside the col_done column fragment
    assert 'id="col-col_done"' in r.text
    assert "Move me" in r.text
    # the moved card's dropdown shows col_done as selected
    assert f'id="card-{card_id}"' in r.text
    assert 'value="col_done" selected' in r.text

    unknown = client.post("/cards/does-not-exist/move", data={"column_id": "col_done"})
    assert unknown.status_code == 404


def test_add_comment(client):
    card_id = _create_card(client, "Comment me")
    r = client.post(
        f"/cards/{card_id}/comments",
        data={"body": "looks good", "author_id": "user_bilal"},
    )
    assert r.status_code == 200
    assert "looks good" in r.text


# --- chat (REST) -----------------------------------------------------------


def test_chat_history_seeded(client):
    r = client.get("/channels/chan_general/messages")
    assert r.status_code == 200
    msgs = r.json()
    assert any(m["author_kind"] == "agent" for m in msgs)


def test_chat_api_renders_safe_standard_markdown(client):
    with client.websocket_connect("/channels/chan_general/ws") as ws:
        ws.send_json({"body": "**bold and *nested italic*** [Docs](https://example.com) <script>alert(1)</script>"})
        message = ws.receive_json()

    rendered = message["rendered_body"]
    assert "<strong>bold and <em>nested italic</em></strong>" in rendered
    assert '<a href="https://example.com">Docs</a>' in rendered
    assert "<script>" not in rendered
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in rendered


def test_agent_routing_ignores_mentions_inside_markdown_quotes():
    body = "> Planner: @planner create the old card\n> quoted continuation\n\nI agree"

    assert agent_routable_text(body) == "I agree"


def test_agent_routing_keeps_mentions_outside_markdown_quotes():
    body = "> Planner: @planner old request\n\n@planner create the new card"

    assert agent_routable_text(body) == "@planner create the new card"


def test_quoted_mention_does_not_trigger_agent_over_websocket(client):
    body = "> Planner: @planner new card \"Do not create\" in Todo\n\nI agree"
    with client.websocket_connect("/channels/chan_general/ws") as ws:
        ws.send_json({"body": body})
        sent = ws.receive_json()

    assert sent["body"] == body
    history = client.get("/channels/chan_general/messages").json()
    assert not any("Do not create" in message["body"] and message["author_kind"] == "agent" for message in history)


def test_chat_page_has_conversation_first_layout(client):
    page = client.get("/channels/chan_general")
    assert page.status_code == 200
    assert 'class="channel-header"' in page.text
    assert 'id="message-list"' in page.text
    assert 'class="message-composer"' in page.text
    assert 'placeholder="Message #general"' in page.text
    assert 'aria-label="Send message"' in page.text
    assert 'class="agent-status"' in page.text
    assert 'id="emoji-button"' in page.text
    assert 'id="emoji-picker"' in page.text
    assert 'id="reaction-picker"' in page.text
    assert 'class="thread-panel"' in page.text
    assert 'id="thread-form"' in page.text
    assert 'title="Quote message"' in page.text
    assert 'const currentMemberId = "user_bilal"' in page.text
    assert '.reaction-row:empty{display:none}' in page.text
    assert 'width:min(78%,700px)' not in page.text
    assert 'id="formatting-button"' in page.text
    assert 'body.innerHTML=m.rendered_body' in page.text
    assert 'className="engagement-row"' in page.text
    assert '.msg.own{grid-template-columns:minmax(0,1fr) 38px;margin:0;width:100%;padding:9px 8px' in page.text


def test_thread_reply_is_kept_out_of_main_timeline(client):
    with client.websocket_connect("/channels/chan_general/ws") as ws:
        ws.send_json({"body": "Thread parent"})
        parent = ws.receive_json()
        assert parent["body"] == "Thread parent"
        assert parent["thread_id"] is None

        ws.send_json({"body": "Thread reply 👍", "thread_id": parent["id"]})
        reply = ws.receive_json()
        assert reply["body"] == "Thread reply 👍"
        assert reply["thread_id"] == parent["id"]

    thread = client.get(f"/channels/chan_general/threads/{parent['id']}")
    assert thread.status_code == 200
    assert [message["body"] for message in thread.json()] == [
        "Thread parent",
        "Thread reply 👍",
    ]

    timeline = client.get("/channels/chan_general/messages").json()
    parent_in_timeline = next(message for message in timeline if message["id"] == parent["id"])
    assert parent_in_timeline["reply_count"] == 1
    assert not any(message["id"] == reply["id"] for message in timeline)


def test_message_reactions_are_persisted_and_toggleable(client):
    with client.websocket_connect("/channels/chan_general/ws") as ws:
        ws.send_json({"body": "React to this"})
        message = ws.receive_json()

    reaction_url = f"/channels/chan_general/messages/{message['id']}/reactions"
    added = client.post(reaction_url, json={"emoji": "❤️"})
    assert added.status_code == 200
    assert added.json() == [{"emoji": "❤️", "count": 1, "reacted": True}]
    assert client.get(reaction_url).json() == added.json()

    removed = client.post(reaction_url, json={"emoji": "❤️"})
    assert removed.status_code == 200
    assert removed.json() == []


# --- chat (websocket) + agent ----------------------------------------------


# --- tools debug endpoint --------------------------------------------------

def test_tools_endpoint_lists_registry(client):
    r = client.get("/tools")
    assert r.status_code == 200
    body = r.json()
    names = {t["name"] for t in body["tools"]}
    assert {"create_card", "move_card", "comment_card", "find_card", "list_columns", "post_message"} <= names
    # Each tool carries a valid object-schema parameters block.
    for t in body["tools"]:
        assert t["input_schema"]["type"] == "object"
        assert "properties" in t["input_schema"]


def test_mentioning_agent_replies_in_a_thread(client):
    with client.websocket_connect("/channels/chan_general/ws") as ws:
        ws.send_json({"body": "@planner help"})
        # A typing frame may arrive first; read past it to the real messages.
        human = agent = None
        for _ in range(6):
            msg = ws.receive_json()
            if msg.get("type") == "typing":
                continue
            if human is None:
                human = msg
            else:
                agent = msg
                break
        assert human is not None and agent is not None
        assert human["body"] == "@planner help"
        assert human["thread_id"] is None

        # Agent's answer is threaded under the human message (keeps the main
        # timeline uncluttered) rather than landing inline in the channel.
        assert agent["author_kind"] == "agent"
        assert agent["thread_id"] == human["id"]

    # The threaded agent reply is excluded from the main timeline...
    timeline = client.get("/channels/chan_general/messages").json()
    assert any(m["id"] == human["id"] for m in timeline)
    assert not any(m["id"] == agent["id"] for m in timeline)
    # ...but it shows up as a reply in the human message's thread.
    thread = client.get(f"/channels/chan_general/threads/{human['id']}").json()
    assert any(m["id"] == agent["id"] for m in thread)


def test_typing_indicator_frame_is_emitted_before_agent_reply(client):
    with client.websocket_connect("/channels/chan_general/ws") as ws:
        ws.send_json({"body": "@planner help"})
        seen = []
        for _ in range(10):
            msg = ws.receive_json()
            seen.append(msg)
            if msg.get("author_kind") == "agent":
                break
        types = [m.get("type") for m in seen]
        # A typing frame (author_id set, no body) precedes the agent's message.
        assert "typing" in types
        typing = next(m for m in seen if m.get("type") == "typing")
        assert typing["author_id"] == "agent_planner"
        assert any(m.get("author_kind") == "agent" for m in seen)


def test_websocket_agent_new_card(client):
    with client.websocket_connect("/channels/chan_general/ws") as ws:
        ws.send_json({"author_id": "user_bilal", "body": '@planner new card "WS test card" in Todo'})
        seen = []
        for _ in range(10):
            msg = ws.receive_json()
            seen.append(msg)
            if "Created card" in msg.get("body", ""):
                break
        bodies = [m["body"] for m in seen if "body" in m]
        assert any("Created card" in b for b in bodies)
        board = client.get("/board/board_main").text
        assert "WS test card" in board


def test_can_open_direct_message_with_agent_and_send_without_mention(client):
    opened = client.get("/direct/agent_planner", follow_redirects=False)

    assert opened.status_code == 303
    channel_url = opened.headers["location"]
    assert channel_url.startswith("/channels/dm_")
    page = client.get(channel_url)
    assert page.status_code == 200
    assert "Direct message" in page.text
    assert "Planner" in page.text

    channel_id = channel_url.rsplit("/")[-1]
    with client.websocket_connect(f"/channels/{channel_id}/ws") as ws:
        ws.send_json({"body": "help"})
        # A typing frame may arrive first; read past it to the real messages.
        human = agent = None
        for _ in range(6):
            msg = ws.receive_json()
            if msg.get("type") == "typing":
                continue
            if human is None:
                human = msg
            else:
                agent = msg
                break

    assert human["body"] == "help"
    assert agent["author_id"] == "agent_planner"
    assert "I can:" in agent["body"]


def test_direct_messages_are_separate_from_workspace_channels(client):
    opened = client.get("/direct/agent_planner", follow_redirects=False)
    channel_id = opened.headers["location"].rsplit("/", 1)[-1]

    page = client.get("/")

    assert f'href="/channels/{channel_id}"' in page.text
    assert 'class="nav-group">Direct messages<' in page.text
    assert f'# Planner' not in page.text
    assert 'href="/direct/agent_planner"' in page.text
