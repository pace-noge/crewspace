"""M9.1 — Structured production logging.

A single, config-driven stdlib `logging` setup. `configure_logging(settings)`
returns a `StreamHandler` (so callers/tests can wire it anywhere) after
configuring the root formatter, log level, and third-party logger tuning.
The `StructuredFormatter` emits either key=value text or single-line JSON
depending on the configured mode.

No dependency on the DB or web layer; pure stdlib logging so it is trivially
testable and safe to call at import/startup time.
"""
from __future__ import annotations

import json
import logging
from typing import Any, Iterable, List

from .config import Settings

# Third-party loggers we deliberately quiet/tune in production.
_QUIET_LOGGERS = ("alembic", "aiosqlite", "uvicorn.access", "uvicorn.error")


class StructuredFormatter(logging.Formatter):
    """Formatter that emits key=value text or single-line JSON per record.

    `mode` ∈ {"text", "json"}. JSON mode emits a flat object with the record's
    `message` (already formatted with args), `levelname`, `name`, `pathname:lineno`,
    and any extra attributes the caller attached via `record.__dict__`.
    """

    _RENDERED_FIELDS = ("message", "levelname", "name", "pathname", "lineno")

    def __init__(self, mode: str = "text") -> None:
        super().__init__()
        self.mode = mode

    def format(self, record: logging.LogRecord) -> str:
        message = record.getMessage()
        extras = _extra_fields(record)
        level = record.levelname or str(record.levelno)
        if self.mode == "json":
            obj: dict[str, Any] = {
                "message": message,
                "levelname": level,
                "name": record.name,
                "pathname": record.pathname,
                "lineno": record.lineno,
            }
            obj.update(extras)
            return json.dumps(obj, default=str)
        # text: key=value, message first
        parts = [f"level={level}", f"logger={record.name}"]
        parts.extend(f"{k}={v}" for k, v in sorted(extras.items()))
        return f"{message} {' '.join(parts)}".strip()


def _extra_fields(record: logging.LogRecord) -> dict[str, Any]:
    """Return caller-attached attributes not part of the standard record."""
    standard = set(logging.LogRecord("", 0, "", 0, "", (), None).__dict__)
    return {k: v for k, v in record.__dict__.items() if k not in standard}


def configure_logging(
    settings: Settings,
    handlers: Iterable[logging.Handler] | None = None,
    quiet_loggers: tuple[str, ...] = _QUIET_LOGGERS,
) -> logging.Handler:
    """Configure root logging from `settings` and return a stream handler.

    - `log_json=True` forces JSON; otherwise `log_format` ("text"/"json") is
      used.
    - `log_level` sets the root effective level.
    - `quiet_loggers` are clamped to WARNING (unless the configured level is
      already below that, in which case it is left alone) — this keeps
      routine third-party noise down while preserving DEBUG intent.

    If `handlers` is provided, the returned handler is NOT attached anywhere;
    the caller owns it (used by tests to capture without mutating global
    state). When `handlers` is omitted the returned stream handler is attached
    to the root logger (production path).
    """
    mode = "json" if settings.log_json else settings.log_format
    handler = _make_handler(mode)
    if handlers is None:
        root = logging.getLogger()
        root.setLevel(_coerce_level(settings.log_level))
        # Replace any pre-existing handlers so we do not duplicate output when
        # configure_logging is called more than once (e.g. uvicorn reload).
        root.handlers = [handler]
        for name in quiet_loggers:
            _clamp_logger(name, settings.log_level)
    else:
        _handlers = list(handlers)
        for _h in _handlers:
            _h.setFormatter(StructuredFormatter(mode))
    return handler


def _make_handler(mode: str) -> logging.Handler:
    h = logging.StreamHandler()
    h.setFormatter(StructuredFormatter(mode))
    return h


def _coerce_level(level: str) -> int:
    level = (level or "INFO").upper()
    numeric = getattr(logging, level, None)
    if isinstance(numeric, int):
        return numeric
    raise ValueError(f"invalid log level: {level!r}")


def _clamp_logger(name: str, configured_level: str) -> None:
    logger = logging.getLogger(name)
    try:
        configured = _coerce_level(configured_level)
    except ValueError:
        configured = logging.INFO
    if logger.level == logging.NOTSET or logger.level > logging.WARNING:
        # Only quiet it if we are not already asking for DEBUG/INFO on it.
        logger.setLevel(max(configured, logging.WARNING))


def format_access_line(
    *,
    method: str,
    path: str,
    status: int,
    duration_ms: float,
    request_id: str,
) -> str:
    """Build a single, greppable access-log line (text form for logfmt)."""
    return (
        f"method={method} path={path} status={status} "
        f"duration_ms={duration_ms:.1f} request_id={request_id}"
    )
