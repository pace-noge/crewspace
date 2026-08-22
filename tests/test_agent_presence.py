"""Live agent presence: connect/disconnect broadcasts to a global presence socket."""

from __future__ import annotations

import asyncio

import pytest


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
