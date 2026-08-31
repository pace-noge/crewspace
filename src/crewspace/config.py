"""Environment-driven configuration via pydantic-settings.

Everything variable (db path, host/port, which agent, LLM creds) is a field
overridable by an env var with the CREWSPACE_ prefix — the "use python env" requirement.
Switching the database later means adding a `db_url` field here and pointing the
infrastructure layer at it; nothing else changes.
"""
from __future__ import annotations

import logging

from pydantic import field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

_DEVELOPMENT_SECRET = "dev-insecure-change-me"
_DEVELOPMENT_PASSWORD = "admin123"

_logger = logging.getLogger("crewspace.config")

# Only these SQLAlchemy URL prefixes are permitted in production.
_ALLOWED_DB_BACKENDS = ("sqlite+", "postgresql+")


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="CREWSPACE_", env_file=".env", extra="ignore")

    app_name: str = "Crewspace"
    host: str = "127.0.0.1"
    port: int = 8000

    # SQLite file used by the default infrastructure. Override in tests.
    db_path: str = "data/crewspace.db"
    # Backend-neutral SQLAlchemy URL. When omitted, db_path remains fully
    # backward compatible and is translated to async SQLite.
    database_url: str | None = None

    # Agent selection + LLM credentials (only needed when agent == "llm").
    agent: str = "stub"  # "stub" | "llm"
    llm_api_key: str | None = None
    llm_base_url: str | None = None
    llm_model: str = "gpt-4o-mini"

    # Auth: a stable secret used to sign session cookies (override in prod).
    secret: str = _DEVELOPMENT_SECRET

    # Default password for the seeded admin (user_bilal). Change after first login.
    seed_admin_password: str = _DEVELOPMENT_PASSWORD

    # How long the app waits for a connected remote agent's reply before giving
    # up (seconds). Remote agents may run long subprocesses (e.g. Claude Code),
    # so this is deliberately generous; a WebSocket stays open regardless.
    agent_reply_timeout: float = 1800.0

    # Structured logging knobs (M9.1). log_format is "text" (key=value) or
    # "json"; log_json is a shorthand that forces JSON regardless of format.
    log_level: str = "INFO"
    log_format: str = "text"
    log_json: bool = False

    @field_validator("port")
    @classmethod
    def port_in_range(cls, v: int) -> int:
        if not 1 <= v <= 65535:
            raise ValueError(f"port must be between 1 and 65535, got {v}")
        return v

    @field_validator("agent_reply_timeout")
    @classmethod
    def reply_timeout_positive(cls, v: float) -> float:
        if v <= 0:
            raise ValueError(f"agent_reply_timeout must be > 0, got {v}")
        return v

    @field_validator("host")
    @classmethod
    def host_not_empty(cls, v: str) -> str:
        if not v or not v.strip():
            raise ValueError("host must not be empty")
        return v

    @model_validator(mode="after")
    def validate_config(self) -> "Settings":
        if self.database_url is None:
            self.database_url = f"sqlite+aiosqlite:///{self.db_path}"
        if self.host not in {"127.0.0.1", "localhost", "::1"} and (
            self.secret == _DEVELOPMENT_SECRET
            or self.seed_admin_password == _DEVELOPMENT_PASSWORD
        ):
            raise ValueError(
                "Set CREWSPACE_SECRET and CREWSPACE_SEED_ADMIN_PASSWORD before binding beyond loopback"
            )
        # Validate that the database URL uses a supported backend (M9.2).
        if self.database_url and not any(self.database_url.startswith(p) for p in _ALLOWED_DB_BACKENDS):
            raise ValueError(
                f"database_url must use a supported backend (sqlite or postgresql); "
                f"got {self.database_url.split('://')[0]!r}"
            )
        # Warn if agent is set to llm but no credentials are provided (M9.2).
        if self.agent == "llm" and not self.llm_api_key:
            _logger.warning(
                "agent=%r but CREWSPACE_LLM_API_KEY is not set — LLM agent will fail "
                "without an API key or equivalent provider credential",
                self.agent,
            )
        return self


def get_settings() -> Settings:
    return Settings()
