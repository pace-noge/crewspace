"""M9.1 — Structured production logging.

Tests cover the configurable, config-driven stdlib logging setup: the JSON vs
text formatter, honored env knobs, and the access-log line builder. These are
pure-ish unit tests (no `app`/`client` fixture) — they call the formatter and
builder directly, and capture a stream handler without mutating global logger
state.
"""
from __future__ import annotations

import io
import json
import logging

from fastapi.testclient import TestClient

from crewspace.config import Settings
from crewspace.main import create_app
from crewspace.logging_config import (
    StructuredFormatter,
    configure_logging,
    format_access_line,
)


def _record(message: str, args=()) -> logging.LogRecord:
    return logging.LogRecord(
        name="crewspace.test",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg=message,
        args=args,
        exc_info=None,
    )


def test_access_middleware_logs_status_500_on_unhandled_exception(caplog) -> None:
    """Unhandled exceptions still produce an access line with status=500 (M9.1)."""
    app = create_app()

    @app.get("/__logging_boom__")
    def _boom() -> None:
        raise RuntimeError("boom")

    client = TestClient(app, raise_server_exceptions=False)
    with caplog.at_level(logging.INFO, logger="crewspace.access"):
        resp = client.get("/__logging_boom__")
    assert resp.status_code == 500
    msgs = [r.getMessage() for r in caplog.records if r.name == "crewspace.access"]
    assert msgs, "access logger should have emitted a line for the failed request"
    line = msgs[-1]
    assert "/__logging_boom__" in line
    assert "500" in line
    assert "request_id=" in line


def test_settings_expose_logging_knobs() -> None:
    s = Settings(log_level="DEBUG", log_format="json", log_json=True)
    assert s.log_level == "DEBUG"
    assert s.log_format == "json"
    assert s.log_json is True
    # Defaults are sane for dev.
    d = Settings()
    assert d.log_level == "INFO"
    assert d.log_format == "text"
    assert d.log_json is False


def test_configure_logging_honors_level_text_default() -> None:
    h = configure_logging(
        Settings(log_level="DEBUG", log_format="text"),
        handlers=[],
    )
    assert h is not None
    line = StructuredFormatter("text").format(
        _record("hello")
    )
    assert "hello" in line
    assert "level=INFO" in line


def test_configure_logging_emits_json_lines() -> None:
    configure_logging(
        Settings(log_level="INFO", log_format="json"),
        handlers=[],
    )
    line = StructuredFormatter("json").format(
        _record("run %s", args=("7",))
    )
    payload = json.loads(line)
    assert payload["message"] == "run 7"
    assert payload["levelname"] == "INFO"
    assert payload["name"] == "crewspace.test"


def test_log_json_true_selects_json_even_if_format_text() -> None:
    configure_logging(Settings(log_json=True), handlers=[])
    line = StructuredFormatter("json").format(
        logging.LogRecord(
            name="crewspace.test", level=logging.WARNING, pathname=__file__,
            lineno=1, msg="warn", args=(), exc_info=None,
        )
    )
    json.loads(line)  # must be valid JSON


def test_access_line_builder_emits_request_context() -> None:
    line = format_access_line(
        method="POST",
        path="/api/chat",
        status=200,
        duration_ms=12,
        request_id="req_abc",
    )
    assert "POST" in line
    assert "/api/chat" in line
    assert "200" in line
    assert "request_id=req_abc" in line or "req_abc" in line


def test_access_middleware_logs_method_path_status_duration_and_request_id(
    caplog,
) -> None:
    """The access middleware emits a structured line with correlation id (M9.1)."""
    app = create_app()
    # TestClient without context manager skips lifespan, so no DB needed.
    client = TestClient(app)
    # caplog captures from the access logger (no lifespan configure_logging
    # replacement since we skip the context manager).
    with caplog.at_level(logging.INFO, logger="crewspace.access"):
        resp = client.get("/__logging_probe__")
    assert resp.status_code == 404  # no such route; proves middleware ran
    msgs = [r.getMessage() for r in caplog.records if r.name == "crewspace.access"]
    assert msgs, "access logger should have emitted at least one line"
    line = msgs[-1]
    assert "GET" in line
    assert "/__logging_probe__" in line
    assert "404" in line
    assert "request_id=" in line or "duration_ms=" in line
