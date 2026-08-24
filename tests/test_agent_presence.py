"""Live agent presence: connect/disconnect broadcasts to a global presence socket."""

from __future__ import annotations

import asyncio
import datetime as dt

import pytest


def _signed_frame(private_key: str, payload: dict) -> dict:
    from crewspace.security import sign_payload

    return {**payload, "sig": sign_payload(private_key, payload)}


def _session_frame(
    private_key: str, session_id: str, seq: int, payload: dict
) -> dict:
    return _signed_frame(
        private_key, {**payload, "session_id": session_id, "seq": seq}
    )


def _register_agent_key(app, public_key: str) -> None:
    async def set_pubkey() -> None:
        async with app.state.db.uow() as uow:
            await uow._conn.execute(
                "UPDATE member SET pubkey=? WHERE id='agent_planner'", (public_key,)
            )

    asyncio.run(set_pubkey())


from crewspace.application.coding_runs import dispatch_coding_run, mark_run_failed


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
                "profile": {
                    "protocol_version": 0,
                    "agent_version": "legacy",
                    "capabilities": ["progress", "tools"],
                    "max_concurrency": 1,
                    "active_runs": 0,
                    "legacy": True,
                },
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


def test_connect_claim_cannot_be_reused(client, app):
    from crewspace.security import generate_agent_keypair, make_connect_claim

    private_key, public_key = generate_agent_keypair()
    _register_agent_key(app, public_key)
    claim = make_connect_claim(private_key, "agent_planner")
    headers = {
        "Authorization": f"Bearer {claim}",
        "Origin": "http://testserver",
    }

    with client.websocket_connect("/agents/ws", headers=headers):
        pass
    with pytest.raises(Exception):
        with client.websocket_connect("/agents/ws", headers=headers):
            pass


def test_legacy_agent_cannot_publish_activity(client, app):
    from crewspace.security import generate_agent_keypair, make_connect_claim

    private_key, public_key = generate_agent_keypair()
    _register_agent_key(app, public_key)
    claim = make_connect_claim(private_key, "agent_planner")
    with client.websocket_connect(
        "/agents/ws",
        headers={
            "Authorization": f"Bearer {claim}",
            "Origin": "http://testserver",
        },
    ) as agent:
        agent.send_json(
            _signed_frame(
                private_key, {"type": "agent_activity", "active_runs": 1}
            )
        )
        assert agent.receive_json() == {
            "type": "error",
            "error": "unsupported capability: agent_activity",
        }


def test_protocol_v1_rejects_replayed_sequence(client, app):
    from crewspace.security import generate_agent_keypair, make_connect_claim

    private_key, public_key = generate_agent_keypair()
    _register_agent_key(app, public_key)
    claim = make_connect_claim(private_key, "agent_planner")
    hello = {
        "type": "hello",
        "protocol_version": 1,
        "agent_version": "replay-test/1",
        "capabilities": [],
        "max_concurrency": 2,
    }
    with client.websocket_connect(
        "/agents/ws",
        headers={"Authorization": f"Bearer {claim}", "Origin": "http://testserver"},
    ) as agent:
        agent.send_json(_signed_frame(private_key, hello))
        session_id = agent.receive_json()["session_id"]
        activity = _session_frame(
            private_key,
            session_id,
            1,
            {"type": "agent_activity", "active_runs": 1},
        )
        agent.send_json(activity)
        assert agent.receive_json()["type"] == "agent_activity_ack"
        agent.send_json(activity)
        assert agent.receive_json() == {
            "type": "error",
            "error": "invalid or replayed sequence",
        }
        agent.send_json(_signed_frame(private_key, hello))
        assert agent.receive_json() == {
            "type": "error",
            "error": "capabilities already negotiated",
        }


def test_replaced_socket_cannot_apply_signed_frames(client, app):
    from crewspace.security import generate_agent_keypair, make_connect_claim

    private_key, public_key = generate_agent_keypair()
    _register_agent_key(app, public_key)
    headers = {"Origin": "http://testserver"}
    old_headers = {
        **headers,
        "Authorization": f"Bearer {make_connect_claim(private_key, 'agent_planner')}",
    }
    new_headers = {
        **headers,
        "Authorization": f"Bearer {make_connect_claim(private_key, 'agent_planner')}",
    }
    with client.websocket_connect("/agents/ws", headers=old_headers) as old_agent:
        with client.websocket_connect("/agents/ws", headers=new_headers):
            old_agent.send_json(
                _signed_frame(
                    private_key, {"type": "agent_activity", "active_runs": 0}
                )
            )
            assert old_agent.receive_json() == {
                "type": "error",
                "error": "stale connection",
            }


def test_agent_negotiates_capabilities_with_signed_hello(client, app):
    from crewspace.api.connection import agent_manager
    from crewspace.security import generate_agent_keypair, make_connect_claim

    private_key, public_key = generate_agent_keypair()

    async def set_pubkey() -> None:
        async with app.state.db.uow() as uow:
            await uow._conn.execute(
                "UPDATE member SET pubkey=? WHERE id='agent_planner'", (public_key,)
            )

    asyncio.run(set_pubkey())
    claim = make_connect_claim(private_key, "agent_planner")
    hello = {
        "type": "hello",
        "protocol_version": 1,
        "agent_version": "test-agent/1.0",
        "capabilities": ["progress", "artifacts"],
        "max_concurrency": 2,
    }

    with client.websocket_connect(
        "/agents/ws",
        headers={"Authorization": f"Bearer {claim}", "Origin": "http://testserver"},
    ) as agent:
        agent.send_json(_signed_frame(private_key, hello))
        acknowledged = agent.receive_json()
        session_id = acknowledged.pop("session_id")
        assert session_id
        assert acknowledged == {
            "type": "hello_ack",
            "protocol_version": 1,
            "capabilities": ["artifacts", "progress"],
            "max_concurrency": 2,
        }
        profile = agent_manager.capability_profile("agent_planner")
        assert profile is not None
        assert profile["agent_version"] == "test-agent/1.0"
        assert profile["legacy"] is False


def test_agent_rejects_invalid_signed_hello_without_losing_legacy_profile(client, app):
    from crewspace.api.connection import agent_manager
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
        agent.send_json(
            _signed_frame(
                private_key,
                {
                    "type": "hello",
                    "protocol_version": 99,
                    "agent_version": "future/1",
                    "capabilities": ["progress"],
                    "max_concurrency": 1,
                },
            )
        )
        assert agent.receive_json() == {
            "type": "error",
            "error": "unsupported protocol version",
        }
        profile = agent_manager.capability_profile("agent_planner")
        assert profile is not None and profile["legacy"] is True


def test_negotiated_agent_cannot_use_unadvertised_progress(client, app):
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
        agent.send_json(
            _signed_frame(
                private_key,
                {
                    "type": "hello",
                    "protocol_version": 1,
                    "agent_version": "reply-only/1",
                    "capabilities": [],
                    "max_concurrency": 1,
                },
            )
        )
        acknowledged = agent.receive_json()
        assert acknowledged["type"] == "hello_ack"
        agent.send_json(
            _session_frame(
                private_key,
                acknowledged["session_id"],
                1,
                {"type": "agent_progress", "message_id": "unknown", "text": "no"},
            )
        )
        assert agent.receive_json() == {
            "type": "error",
            "error": "unsupported capability: progress",
        }


def test_agent_sends_signed_busy_slot_update(client, app):
    from crewspace.api.connection import agent_manager
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
        agent.send_json(
            _signed_frame(
                private_key,
                {
                    "type": "hello",
                    "protocol_version": 1,
                    "agent_version": "busy/1",
                    "capabilities": ["progress"],
                    "max_concurrency": 2,
                },
            )
        )
        acknowledged = agent.receive_json()
        agent.send_json(
            _session_frame(
                private_key,
                acknowledged["session_id"],
                1,
                {"type": "agent_activity", "active_runs": 1},
            )
        )
        assert agent.receive_json() == {
            "type": "agent_activity_ack",
            "active_runs": 1,
            "max_concurrency": 2,
        }
        assert agent_manager.capability_profile("agent_planner")["active_runs"] == 1


def test_negotiated_profile_is_visible_in_management(client, app):
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
        agent.send_json(
            _signed_frame(
                private_key,
                {
                    "type": "hello",
                    "protocol_version": 1,
                    "agent_version": "visible-agent/2.1",
                    "capabilities": ["progress", "artifacts"],
                    "max_concurrency": 2,
                },
            )
        )
        acknowledged = agent.receive_json()
        agent.send_json(
            _session_frame(
                private_key,
                acknowledged["session_id"],
                1,
                {"type": "agent_activity", "active_runs": 1},
            )
        )
        agent.receive_json()

        response = client.get("/management")
        assert response.status_code == 200
        assert "visible-agent/2.1" in response.text
        assert "1/2 slots" in response.text
        assert "artifacts" in response.text


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


def test_agent_ws_coding_run_failed_transitions_run_through_wired_path(client, app):
    # Drives the REAL agent_ws socket (not the helper directly) so the wired
    # `await _handle_coding_run_failed` call path is exercised.
    from crewspace.dto.change_sets import (
        ChangeArtifactDTO,
        ChangeCommitDTO,
        ChangedFileDTO,
        ChangeSetDTO,
        VerificationResultDTO,
    )
    from crewspace.security import generate_agent_keypair, make_connect_claim

    private_key, public_key = generate_agent_keypair()

    async def setup() -> str:
        async with app.state.db.uow() as uow:
            await uow._conn.execute(
                "UPDATE member SET pubkey=? WHERE id='agent_planner'", (public_key,)
            )
            await uow.coding_repositories.grant_team(
                TeamRepositoryAccess(
                    team_id="team_acme",
                    repository_id="repo_wired_fail",
                    granted_by="user_bilal",
                    granted_at=dt.datetime.now(dt.timezone.utc),
                )
            )
            run = await dispatch_coding_run(
                uow,
                agent_id="agent_planner",
                team_id="team_acme",
                repository_id="repo_wired_fail",
                run_id="run_wired_fail",
                instruction="work",
                requested_by="user_bilal",
                agent_manager=_FakeManager(),
            )
        return run.request_id

    from crewspace.domain.entities import TeamRepositoryAccess

    class _FakeManager:
        async def send_coding_run(self, *a, **k):
            return

        async def send_coding_cancel(self, *a, **k):
            return

    request_id = asyncio.run(setup())

    claim = make_connect_claim(private_key, "agent_planner")

    with client.websocket_connect(
        "/agents/ws",
        headers={
            "Authorization": f"Bearer {claim}",
            "Origin": "http://testserver",
        },
    ) as agent:
        # Negotiate capabilities (incl. coding_workspace) before sending frames.
        hello = {
            "type": "hello",
            "protocol_version": 1,
            "agent_version": "test-agent/1.0",
            "capabilities": ["progress", "coding_workspace", "cancellation"],
            "max_concurrency": 1,
        }
        agent.send_json(_signed_frame(private_key, hello))
        ack = agent.receive_json()  # hello_ack
        session_id = ack["session_id"]
        # Send a signed coding_run_failed frame through the REAL socket.
        payload = {
            "type": "coding_run_failed",
            "request_id": request_id,
            "error": "boom",
            "session_id": session_id,
            "seq": 1,
        }
        agent.send_json(_signed_frame(private_key, payload))
        # Allow the server to process the frame.
        import time

        time.sleep(0.4)

    async def status() -> str:
        async with app.state.db.uow() as uow:
            run = await uow.coding_runs.get("run_wired_fail")
            return run.status if run else "missing"

    assert asyncio.run(status()) == "failed"
