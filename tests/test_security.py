"""Security boundary regression tests."""
from __future__ import annotations

import pytest
from pydantic import ValidationError
from starlette.websockets import WebSocketDisconnect

from crewspace.config import Settings


def test_non_loopback_bind_rejects_shipped_development_credentials():
    with pytest.raises(ValidationError):
        Settings(host="0.0.0.0")


def test_non_loopback_bind_accepts_explicit_secure_credentials():
    settings = Settings(
        host="0.0.0.0",
        secret="deployment-specific-secret-with-sufficient-entropy",
        seed_admin_password="deployment-specific-admin-password",
    )

    assert settings.host == "0.0.0.0"


def test_anonymous_management_request_is_rejected(anonymous_client):
    response = anonymous_client.get("/management")

    assert response.status_code == 401


def test_anonymous_chat_websocket_is_rejected(anonymous_client):
    with pytest.raises(WebSocketDisconnect) as exc_info:
        with anonymous_client.websocket_connect(
            "/channels/chan_general/ws", headers={"Origin": "http://testserver"}
        ):
            pass

    assert exc_info.value.code == 4001


def test_cross_origin_login_is_rejected(anonymous_client):
    response = anonymous_client.post(
        "/auth/login",
        data={"username": "Bilal", "password": "admin123"},
        headers={"Origin": "https://attacker.example"},
    )

    assert response.status_code == 403


def test_cookie_authenticated_post_requires_same_origin(client):
    response = client.post(
        "/channels/chan_general/messages/nonexistent/reactions",
        data={"emoji": "👍"},
        headers={"Origin": "https://attacker.example"},
    )

    assert response.status_code == 403


def test_non_browser_api_request_without_cookie_does_not_require_origin(anonymous_client):
    response = anonymous_client.post(
        "/tools/run", json={"tool": "missing", "arguments": {}}
    )

    assert response.status_code != 403


def test_signed_remote_agent_can_connect_from_different_origin(anonymous_client, app):
    import asyncio

    from crewspace.security import generate_agent_keypair, make_connect_claim

    private_key, public_key = generate_agent_keypair()

    async def register_key():
        async with app.state.db.uow() as uow:
            await uow._conn.execute(
                "UPDATE member SET pubkey=? WHERE id='agent_planner'", (public_key,)
            )

    asyncio.run(register_key())
    claim = make_connect_claim(private_key, "agent_planner")

    with anonymous_client.websocket_connect(
        "/agents/ws",
        headers={
            "Authorization": f"Bearer {claim}",
            "Origin": "https://remote-agent.example",
        },
    ) as websocket:
        websocket.send_json({"type": "probe"})
        response = websocket.receive_json()

    assert response == {"type": "error", "error": "bad signature"}


def test_chat_websocket_rejects_cross_origin_client(client):
    with pytest.raises(WebSocketDisconnect) as exc_info:
        with client.websocket_connect(
            "/channels/chan_general/ws",
            headers={"Origin": "https://attacker.example"},
        ):
            pass

    assert exc_info.value.code == 4003


def test_chat_websocket_cleans_up_room_after_malformed_frame(client):
    from json import JSONDecodeError

    from crewspace.api.connection import manager

    with pytest.raises(JSONDecodeError):
        with client.websocket_connect("/channels/chan_general/ws") as websocket:
            websocket.send_text("not-json")
            websocket.receive_json()

    assert not manager._rooms.get("chan_general")


def test_login_page_does_not_disclose_seeded_credentials(anonymous_client):
    response = anonymous_client.get("/auth/login")

    assert response.status_code == 200
    assert "admin123" not in response.text
    assert "Default admin" not in response.text


def test_successful_login_redirects_home_and_login_page_does_not_trap_session(
    anonymous_client,
):
    anonymous_client.headers["Origin"] = "http://testserver"
    response = anonymous_client.post(
        "/auth/login",
        data={"username": "Bilal", "password": "admin123"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert response.headers["location"] == "/"
    assert response.cookies.get("crewspace_session")

    login_page = anonymous_client.get("/auth/login", follow_redirects=False)
    assert login_page.status_code == 303
    assert login_page.headers["location"] == "/"


def test_outsider_cannot_read_or_toggle_channel_reactions(client):
    with client.websocket_connect("/channels/chan_general/ws") as ws:
        ws.send_json({"body": "Private reaction target"})
        message = ws.receive_json()

    registered = client.post(
        "/auth/register",
        data={"username": "Reaction Outsider", "password": "outsider-password"},
        follow_redirects=False,
    )
    assert registered.status_code == 303
    url = f"/channels/chan_general/messages/{message['id']}/reactions"

    assert client.get(url).status_code == 404
    assert client.post(url, json={"emoji": "👍"}).status_code == 404


def test_outsider_cannot_read_or_create_cards_on_foreign_board(client):
    registered = client.post(
        "/auth/register",
        data={"username": "Board Outsider", "password": "outsider-password"},
        follow_redirects=False,
    )
    assert registered.status_code == 303

    assert client.get("/board/board_main").status_code == 404
    assert client.get("/boards/board_main").status_code == 404
    assert client.get("/boards/board_main/columns/col_todo").status_code == 404
    assert client.post(
        "/boards/board_main/cards",
        data={"column_id": "col_todo", "title": "Unauthorized card"},
    ).status_code == 404


def test_outsider_cannot_move_or_comment_on_foreign_card(client, app):
    import asyncio

    async def first_card_id() -> str:
        async with app.state.db.uow() as uow:
            row = await (
                await uow._conn.execute(
                    "SELECT id FROM card ORDER BY position LIMIT 1"
                )
            ).fetchone()
            return row["id"]

    card_id = asyncio.run(first_card_id())
    registered = client.post(
        "/auth/register",
        data={"username": "Card Outsider", "password": "outsider-password"},
        follow_redirects=False,
    )
    assert registered.status_code == 303

    assert client.post(
        f"/cards/{card_id}/move", data={"column_id": "col_doing"}
    ).status_code == 404
    assert client.post(
        f"/cards/{card_id}/comments", data={"body": "Unauthorized comment"}
    ).status_code == 404


def test_create_card_rejects_column_from_another_board(client, app):
    import asyncio

    async def add_other_board() -> None:
        async with app.state.db.uow() as uow:
            await uow._conn.execute(
                "INSERT INTO board(id,workspace_id,name) VALUES('board_other','ws_default','Other')"
            )
            await uow._conn.execute(
                "INSERT INTO board_column(id,board_id,name,position) VALUES('col_other','board_other','Other',0)"
            )

    asyncio.run(add_other_board())
    response = client.post(
        "/boards/board_main/cards",
        data={"column_id": "col_other", "title": "Wrong board"},
    )

    assert response.status_code == 404


def test_remote_agent_cannot_override_cronjob_creator(app):
    import asyncio

    from crewspace.api.routers.agents import _run_tool
    from crewspace.application.tools import build_registry

    class ToolSocket:
        def __init__(self) -> None:
            self.result = None

        async def send_json(self, payload: dict) -> None:
            self.result = payload

    async def run_spoof_attempt():
        socket = ToolSocket()
        socket.app = app
        await _run_tool(
            socket,
            build_registry(),
            "agent_planner",
            {
                "call_id": "spoof",
                "name": "create_cronjob",
                "args": {
                    "name": "Spoofed creator",
                    "channel_id": "chan_general",
                    "instruction": "Do something later",
                    "schedule_kind": "interval",
                    "interval_value": "1",
                    "interval_unit": "hours",
                    "creator_id": "user_bilal",
                },
            },
        )
        async with app.state.db.uow() as uow:
            row = await (
                await uow._conn.execute(
                    "SELECT creator_id FROM scheduled_job WHERE name='Spoofed creator'"
                )
            ).fetchone()
        return socket.result, row

    result, row = asyncio.run(run_spoof_attempt())

    assert "error" not in result["result"]
    assert row["creator_id"] == "agent_planner"


def test_remote_agent_cannot_mutate_foreign_board_or_channel(app):
    import asyncio

    from crewspace.api.routers.agents import _run_tool
    from crewspace.application.tools import build_registry

    class ToolSocket:
        def __init__(self) -> None:
            self.result = None
            self.app = app

        async def send_json(self, payload: dict) -> None:
            self.result = payload

    async def attempt(tool_name: str, args: dict):
        socket = ToolSocket()
        await _run_tool(
            socket, build_registry(), "agent_planner",
            {"call_id": tool_name, "name": tool_name, "args": args},
        )
        return socket.result["result"]

    async def arrange_and_attempt():
        async with app.state.db.uow() as uow:
            await uow._conn.execute(
                "INSERT INTO team(id,name,created_by,created_at) VALUES('team_foreign','Foreign','user_bilal','2026-01-01T00:00:00+00:00')"
            )
            await uow._conn.execute(
                "INSERT INTO workspace(id,team_id,name,created_by,created_at) VALUES('ws_foreign','team_foreign','Foreign','user_bilal','2026-01-01T00:00:00+00:00')"
            )
            await uow._conn.execute(
                "INSERT INTO channel(id,workspace_id,name,channel_type,mention_policy,created_by,created_at) VALUES('chan_foreign','ws_foreign','foreign','permanent','channel_members','user_bilal','2026-01-01T00:00:00+00:00')"
            )
            await uow._conn.execute(
                "INSERT INTO board(id,workspace_id,name) VALUES('board_foreign','ws_foreign','Foreign')"
            )
            await uow._conn.execute(
                "INSERT INTO board_column(id,board_id,name,position) VALUES('col_foreign','board_foreign','Todo',0)"
            )
        return (
            await attempt("create_card", {"column_id": "col_foreign", "title": "Intrusion"}),
            await attempt("post_message", {"channel_id": "chan_foreign", "body": "Intrusion"}),
        )

    card_result, message_result = asyncio.run(arrange_and_attempt())

    assert "PermissionError" in card_result["error"]
    assert "PermissionError" in message_result["error"]


def test_agent_private_key_response_escapes_name_and_disables_caching(client):
    response = client.post(
        "/auth/agents/register",
        data={
            "name": '<script id="stolen">alert(1)</script>',
            "avatar": "🤖",
            "base_url": "",
            "backend": "stub",
        },
    )

    assert response.status_code == 200
    assert '<script id="stolen">' not in response.text
    assert "&lt;script id=&#34;stolen&#34;&gt;" in response.text
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["referrer-policy"] == "no-referrer"
