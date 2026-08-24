"""Environment-driven configuration via pydantic-settings.

Everything variable (db path, host/port, which agent, LLM creds) is a field
overridable by an env var with the CREWSPACE_ prefix — the "use python env" requirement.
Switching the database later means adding a `db_url` field here and pointing the
infrastructure layer at it; nothing else changes.
"""
from __future__ import annotations

from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

_DEVELOPMENT_SECRET = "dev-insecure-change-me"
_DEVELOPMENT_PASSWORD = "admin123"


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

    # Coding repositories are operator-configured and addressed by opaque IDs;
    # agents never provide filesystem paths. Keep managed worktrees outside the
    # source repositories so a coding run cannot mutate the app checkout.
    coding_repositories: dict[str, str] = {}
    coding_worktree_root: str = "~/.local/share/crewspace/worktrees"

    @model_validator(mode="after")
    def reject_network_exposure_with_development_credentials(self) -> "Settings":
        if self.database_url is None:
            self.database_url = f"sqlite+aiosqlite:///{self.db_path}"
        if self.host not in {"127.0.0.1", "localhost", "::1"} and (
            self.secret == _DEVELOPMENT_SECRET
            or self.seed_admin_password == _DEVELOPMENT_PASSWORD
        ):
            raise ValueError(
                "Set CREWSPACE_SECRET and CREWSPACE_SEED_ADMIN_PASSWORD before binding beyond loopback"
            )
        return self


def get_settings() -> Settings:
    return Settings()
