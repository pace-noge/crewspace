"""M9.2 — Runtime config hardening + validation.

Tests assert that invalid production settings are rejected at construction
(fail-fast), valid production-shaped settings pass, and existing dev defaults
remain valid.
"""
from __future__ import annotations

import pytest

from crewspace.config import Settings


def test_valid_production_settings_pass() -> None:
    s = Settings(
        host="0.0.0.0",
        port=8080,
        secret="a-strong-production-secret-1234",
        seed_admin_password="a-strong-admin-pw-1234",
        database_url="postgresql+asyncpg://u:pw@localhost/crewspace",
        agent="llm",
        llm_api_key="sk-abc123",
        agent_reply_timeout=3600.0,
    )
    assert s.port == 8080
    assert s.database_url == "postgresql+asyncpg://u:pw@localhost/crewspace"


def test_defaults_are_valid() -> None:
    s = Settings()
    assert s.host == "127.0.0.1"
    assert s.port == 8000
    assert s.agent_reply_timeout == 1800.0
    assert s.secret == "dev-insecure-change-me"


def test_port_out_of_range_rejected() -> None:
    with pytest.raises(Exception):
        Settings(port=0)
    with pytest.raises(Exception):
        Settings(port=65536)
    with pytest.raises(Exception):
        Settings(port=-1)


def test_reply_timeout_nonpositive_rejected() -> None:
    with pytest.raises(Exception):
        Settings(agent_reply_timeout=0)
    with pytest.raises(Exception):
        Settings(agent_reply_timeout=-5.0)


def test_invalid_db_backend_rejected() -> None:
    with pytest.raises(Exception):
        Settings(database_url="mysql+pymysql://root@localhost/cw")


def test_empty_host_rejected() -> None:
    with pytest.raises(Exception):
        Settings(host="")


def test_llm_agent_warns_without_api_key(caplog) -> None:
    """When agent=llm, a missing llm_api_key should log a warning (not hard fail)."""
    import logging
    with caplog.at_level(logging.WARNING):
        Settings(agent="llm", llm_api_key=None, llm_base_url=None)
    assert any("llm_api_key" in r.message.lower() for r in caplog.records)
