"""M9.6 — release and deployment documentation acceptance tests."""
from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

DOCS_DIR = Path("docs")
DEPLOYMENT = DOCS_DIR / "DEPLOYMENT.md"
RELEASING = DOCS_DIR / "RELEASING.md"


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _env_names_in_markdown(text: str) -> set[str]:
    return {m.group(0) for m in re.finditer(r"CREWSPACE_[A-Z0-9_]+", text)}


def _settings_fields() -> set[str]:
    src = Path("src/crewspace/config.py")
    tree = ast.parse(src.read_text(encoding="utf-8"), filename=str(src))
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == "Settings":
            return {
                target.id
                for item in node.body
                if isinstance(item, ast.AnnAssign) and isinstance(item.target, ast.Name)
                for target in [item.target]
            }
    raise AssertionError("Settings class not found in config.py")


def _settings_env_names() -> set[str]:
    """Return the environment names Settings accepts, e.g. CREWSPACE_SECRET."""
    return {f"CREWSPACE_{field.upper()}" for field in _settings_fields()}


# --- Acceptance 1: both docs exist ---


def test_deployment_doc_exists() -> None:
    assert DEPLOYMENT.exists(), f"{DEPLOYMENT} must exist"


def test_releasing_doc_exists() -> None:
    assert RELEASING.exists(), f"{RELEASING} must exist"


# --- Acceptance 2: security-critical env vars covered in DEPLOYMENT.md ---


_DEPLOYMENT_SECURITY_VARS = {
    "CREWSPACE_SECRET",
    "CREWSPACE_SEED_ADMIN_PASSWORD",
    "CREWSPACE_DATABASE_URL",
    "CREWSPACE_LOG_LEVEL",
}


def test_deployment_covers_security_critical_env_vars() -> None:
    text = _read(DEPLOYMENT)
    mentioned = _env_names_in_markdown(text)
    missing = _DEPLOYMENT_SECURITY_VARS - mentioned
    assert not missing, f"DEPLOYMENT.md must mention security-critical vars: {missing}"


def test_deployment_warns_about_dev_defaults_on_non_loopback() -> None:
    text = _read(DEPLOYMENT).lower()
    assert "loopback" in text, "DEPLOYMENT.md must warn about dev defaults on non-loopback"


# --- Acceptance 3: env-drift guard (doc env names ⊆ Settings fields) ---


def test_deployment_env_names_match_settings_fields() -> None:
    text = _read(DEPLOYMENT)
    doc_names = _env_names_in_markdown(text)
    settings_env_names = _settings_env_names()
    drift = doc_names - settings_env_names
    assert not drift, f"DEPLOYMENT.md references env names not in Settings: {drift}"


# --- Acceptance 4: releasing doc describes versioning/tagging/verified-slice ---


def test_releasing_describes_versioning_and_tagging() -> None:
    text = _read(RELEASING).lower()
    for keyword in ("version", "tag", "milestone"):
        assert keyword in text, f"RELEASING.md must describe {keyword}"


def test_releasing_describes_verified_slice_flow() -> None:
    text = _read(RELEASING).lower()
    assert "verified" in text or "red" in text, (
        "RELEASING.md must describe the verified-slice commit flow"
    )
