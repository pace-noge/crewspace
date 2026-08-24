"""Live agent presence: connect/disconnect broadcasts to a global presence socket."""

from __future__ import annotations

import asyncio

import pytest


def _signed_frame(private_key: str, payload: dict) -> dict:
    from crewspace.security import sign_payload

    return {**payload, "sig": sign_payload(private_key, payload)}


def test_presence_socket_receives_agent_connect_and_disconnect(client, app):
    from crewspace.security import generate_agent_keypair, make_connect_claim

    private_key, public_key = generate_agent_keypair()

    async def set_pubkey() -> None:
        async with app.state.db.uow() as uow:
            await uow._conn.execute(
                "UPDATE member SET pubkey=? WHERE id='agent_planner'", (public_key,)
            )

    asyncio.run(set_pubkey())
    claim = make_connect_claim(private_key, "agent_planner")

    with client.websocket_connect(
        "/ws/presence", headers={"Origin": "http://testserver"}
    ) as presence:
        with client.websocket_connect(
            "/agents/ws",
            headers={
                "Authorization": f"Bearer {claim}",
                "Origin": "http://testserver",
            },
        ) as _agent:
            # A connected remote agent broadcasts a "connected" presence frame.
            connected = presence.receive_json()
            assert connected == {
                "type": "agent_presence",
                "agent_id": "agent_planner",
                "status": "connected",
            }
        # Closing the agent socket broadcasts a "disconnected" frame.
        disconnected = presence.receive_json()
        assert disconnected == {
            "type": "agent_presence",
            "agent_id": "agent_planner",
            "status": "disconnected",
        }


def test_presence_socket_rejects_cross_origin(anonymous_client):
    with pytest.raises(Exception):
        with anonymous_client.websocket_connect(
            "/ws/presence", headers={"Origin": "https://attacker.example"}
        ):
            pass


def test_signed_agent_progress_reaches_channel_before_final_reply(client, app):
    from crewspace.security import generate_agent_keypair, make_connect_claim

    private_key, public_key = generate_agent_keypair()

    async def set_pubkey() -> None:
        async with app.state.db.uow() as uow:
            await uow._conn.execute(
                "UPDATE member SET pubkey=? WHERE id='agent_planner'", (public_key,)
            )

    asyncio.run(set_pubkey())
    claim = make_connect_claim(private_key, "agent_planner")

    with client.websocket_connect(
        "/agents/ws",
        headers={"Authorization": f"Bearer {claim}", "Origin": "http://testserver"},
    ) as agent:
        with client.websocket_connect(
            "/channels/chan_general/ws", headers={"Origin": "http://testserver"}
        ) as chat:
            chat.send_json({"body": "@planner stream this"})
            assert chat.receive_json()["body"] == "@planner stream this"
            assert chat.receive_json()["type"] == "typing"
            assert chat.receive_json()["type"] == "agent_working"

            request = agent.receive_json()
            message_id = request["message_id"]
            agent.send_json(
                _signed_frame(
                    private_key,
                    {"type": "agent_progress", "message_id": message_id, "text": "line 1"},
                )
            )
            progress = chat.receive_json()
            assert progress == {
                "type": "agent_progress",
                "author_id": "agent_planner",
                "channel_id": "chan_general",
                "message_id": message_id,
                "text": "line 1",
            }

            agent.send_json(
                _signed_frame(
                    private_key,
                    {"type": "reply", "message_id": message_id, "text": "done"},
                )
            )
            completed = chat.receive_json()
            assert completed == {
                "type": "agent_progress_complete",
                "author_id": "agent_planner",
                "channel_id": "chan_general",
                "message_id": message_id,
            }
            reply = chat.receive_json()
            assert reply["author_id"] == "agent_planner"
            assert reply["body"] == "done"
