"""End-to-end smoke test for the builtin app-LLM assistant (Crewspace).

This test proves the builtin agent (pubkey NULL, backend ``llm``,
``uses_app_llm=1``) actually reaches the server's LLM credentials and returns a
real reply — i.e. that the key you put in ``CREWSPACE_LLM_API_KEY`` /
``CREWSPACE_LLM_BASE_URL`` / ``CREWSPACE_LLM_MODEL`` is wired through.

It is SKIPPED unless a real key is present (so the default suite stays offline
and free). Run it with:

    CREWSPACE_LLM_API_KEY=sk-... uv run pytest -q tests/test_builtin_llm_smoke.py

Optionally set ``CREWSPACE_LLM_BASE_URL`` and ``CREWSPACE_LLM_MODEL``.
"""

from __future__ import annotations

import asyncio
import os
import tempfile
from pathlib import Path

import pytest

from crewspace.config import Settings
from crewspace.domain.identifiers import BUILTIN_ASSISTANT_ID
from crewspace.infrastructure.agents.registry import AgentRegistry
from crewspace.infrastructure.db import Database

# The app reads the key from the environment / .env via pydantic-settings
# (Settings loads .env automatically). We mirror that here so the test runs
# whenever a real key is configured, not just when it's exported to os.environ.
_settings = Settings()
pytestmark = pytest.mark.skipif(
    not _settings.llm_api_key,
    reason="no CREWSPACE_LLM_API_KEY set (via env or .env); builtin LLM smoke test is key-gated",
)


@pytest.mark.asyncio
async def test_builtin_assistant_replies_via_app_llm():
    settings = Settings(db_path=str(Path(tempfile.mkdtemp()) / "smoke.db"))
    # The builtin assistant must read the server LLM creds from the environment.
    assert settings.llm_api_key, "llm_api_key should come from CREWSPACE_LLM_API_KEY"

    database = await Database.create(settings)
    try:
        async with database.uow() as uow:
            registry = await AgentRegistry.build(settings, uow)
            runner = (await _tool_registry()).bind(uow)

            agent_id, replies = await registry.on_chat_message(
                f"@crewspace say hello in one short sentence", runner
            )

    finally:
        await database.close()

    assert agent_id == BUILTIN_ASSISTANT_ID, (
        f"expected the builtin assistant ({BUILTIN_ASSISTANT_ID}) to answer, "
        f"got {agent_id!r}"
    )
    assert replies, "builtin assistant returned no reply"
    body = " ".join(replies)
    # A stub agent answers with "Got it. Mention `@crewspace help` ..."; a real
    # app-LLM answer must NOT match that canned fallback.
    assert "Mention `@crewspace help`" not in body, (
        "builtin assistant used the STUB provider, not the app LLM — check the "
        "member.backend column for agent_crewspace is 'llm'"
    )
    assert not body.startswith("⚠️"), f"builtin assistant errored: {body}"


async def _tool_registry():
    # Imported lazily to keep the offline suite import-cheap.
    from crewspace.application.tools import build_registry

    return build_registry()


@pytest.mark.asyncio
async def test_builtin_assistant_uses_thread_context():
    """The builtin agent must see prior conversation and ground its answer in it.

    We seed a short thread, then ask the agent to summarize it. A real app-LLM
    reply should reference the seeded content (proving context was supplied),
    and must NOT be the stub fallback or an error.
    """
    from crewspace.domain.identifiers import DEFAULT_CHANNEL_ID

    settings = Settings(db_path=str(Path(tempfile.mkdtemp()) / "ctx.db"))
    assert settings.llm_api_key, "llm_api_key should come from CREWSPACE_LLM_API_KEY"

    database = await Database.create(settings)
    try:
        async with database.uow() as uow:
            # Seed a thread root + one reply so there is history to summarize.
            root = await uow.chat.add_message(DEFAULT_CHANNEL_ID, "user_bilal", "Plan the launch", None)
            await uow.chat.add_message(DEFAULT_CHANNEL_ID, "user_bilal", "Email the team", root.id)
            await uow.commit()

            runner = (await _tool_registry()).bind(uow)
            registry = await AgentRegistry.build(settings, uow)

            # Agent replies live in a thread under the human message; build the
            # same context the live service would (whole thread) and ask to summarize.
            history = await uow.chat.list_thread(root.id)
            context = [
                {"role": "assistant" if m.author_kind == "agent" else "user",
                 "name": m.author_name, "content": m.body}
                for m in history
            ]
            agent_id, replies = await registry.on_chat_message(
                f"@crewspace summarize this thread", runner, context=context
            )
    finally:
        await database.close()

    assert agent_id == BUILTIN_ASSISTANT_ID
    assert replies, "builtin assistant returned no reply"
    body = " ".join(replies)
    assert "Mention `@crewspace help`" not in body, "stub provider used, not app LLM"
    assert not body.startswith("⚠️"), f"builtin assistant errored: {body}"
    # Context flowed: the reply should echo at least one seeded phrase.
    assert "launch" in body.lower() or "email" in body.lower(), (
        f"agent reply did not reference the thread context: {body!r}"
    )
