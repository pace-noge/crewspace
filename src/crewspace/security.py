"""Security primitives — password hashing + signed session/agent tokens.

Stdlib-only (no extra dependencies). Passwords use PBKDF2-HMAC-SHA256 with a
random salt; session tokens and agent connect-tokens are HMAC-signed so they
can't be forged without the server secret (CREWSPACE_SECRET). Keep this module free of
framework/DB imports.
"""
from __future__ import annotations

import hashlib
import hmac
import os
import secrets
from urllib.parse import urlsplit

_PBKDF2_ROUNDS = 200_000


def is_same_origin(origin: str | None, target_url: str) -> bool:
    """Return whether a browser Origin matches the request's public origin."""
    if not origin:
        return False
    source = urlsplit(origin)
    target = urlsplit(target_url)
    target_scheme = {"ws": "http", "wss": "https"}.get(target.scheme, target.scheme)
    source_port = source.port or (443 if source.scheme == "https" else 80)
    target_port = target.port or (443 if target_scheme == "https" else 80)
    return (
        source.scheme == target_scheme
        and source.hostname == target.hostname
        and source_port == target_port
    )


def hash_password(password: str) -> str:
    """Return a self-contained hashed-password string: ``pbkdf2$<salt>$<hash>``."""
    salt = secrets.token_bytes(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, _PBKDF2_ROUNDS)
    return "pbkdf2$" + salt.hex() + "$" + dk.hex()


def verify_password(password: str, stored: str) -> bool:
    """Constant-time verify against a ``hash_password`` output."""
    try:
        algo, salt_hex, hash_hex = stored.split("$")
    except ValueError:
        return False
    if algo != "pbkdf2":
        return False
    salt = bytes.fromhex(salt_hex)
    expected = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, _PBKDF2_ROUNDS)
    return hmac.compare_digest(expected.hex(), hash_hex)


def new_session_id() -> str:
    return secrets.token_urlsafe(32)


def sign_session(session_id: str, secret: str) -> str:
    """HMAC-sign a session id so the cookie value can't be tampered with."""
    sig = hmac.new(secret.encode("utf-8"), session_id.encode("utf-8"), hashlib.sha256).hexdigest()
    return f"{session_id}.{sig}"


def unsign_session(token: str, secret: str) -> str | None:
    """Return the session id if the token's signature is valid, else None."""
    try:
        session_id, sig = token.split(".", 1)
    except ValueError:
        return None
    expected = hmac.new(secret.encode("utf-8"), session_id.encode("utf-8"), hashlib.sha256).hexdigest()
    if hmac.compare_digest(expected, sig):
        return session_id
    return None


# --- agent connect tokens -------------------------------------------------
# A remote agent process authenticates its WebSocket with a token derived from
# its member id. Stateless: the server re-derives and verifies it with CREWSPACE_SECRET,
# so there is no token column to store or rotate.
def agent_token(agent_id: str, secret: str) -> str:
    sig = hmac.new(secret.encode("utf-8"), agent_id.encode("utf-8"), hashlib.sha256).hexdigest()
    return f"{agent_id}.{sig}"


def verify_agent_token(token: str, secret: str) -> str | None:
    """Return the agent id if the connect token is valid, else None."""
    try:
        agent_id, sig = token.split(".", 1)
    except ValueError:
        return None
    expected = hmac.new(secret.encode("utf-8"), agent_id.encode("utf-8"), hashlib.sha256).hexdigest()
    if hmac.compare_digest(expected, sig):
        return agent_id
    return None


# --- agent identity: Ed25519 keypairs (Buzz-style signed actions) ----------
# Each agent member owns an Ed25519 keypair. The PUBLIC key is stored server-side
# (member.pubkey); the PRIVATE key is handed to the agent once at registration
# and never stored by the server. A remote agent proves its identity by signing a
# connect claim + signs every action it takes, so the server can verify both
# "who" (the registered agent) and "that the action is authentic" (non-repudiable).
import base64
import json
import time

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey
from cryptography.exceptions import InvalidSignature

_CONNECT_TTL = 60  # seconds a connect claim is valid


def _b64u(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def _b64u_decode(s: str) -> bytes:
    pad = "=" * (-len(s) % 4)
    return base64.urlsafe_b64decode(s + pad)


def _canonical(payload: dict) -> bytes:
    return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")


def generate_agent_keypair() -> tuple[str, str]:
    """Return (private_key_b64url, public_key_b64url) for a new agent."""
    priv = Ed25519PrivateKey.generate()
    pub = priv.public_key()
    return _b64u(priv.private_bytes_raw()), _b64u(pub.public_bytes_raw())


def _priv_from_b64u(b64u_str: str) -> Ed25519PrivateKey:
    return Ed25519PrivateKey.from_private_bytes(_b64u_decode(b64u_str))


def _pub_from_b64u(b64u_str: str) -> Ed25519PublicKey:
    return Ed25519PublicKey.from_public_bytes(_b64u_decode(b64u_str))


def sign_payload(priv_b64u: str, payload: dict) -> str:
    """Ed25519-sign a JSON-canonical payload; return base64url signature."""
    priv = _priv_from_b64u(priv_b64u)
    return _b64u(priv.sign(_canonical(payload)))


def verify_payload(pub_b64u: str, payload: dict, sig_b64u: str) -> bool:
    """Verify an Ed25519 signature over a JSON-canonical payload."""
    try:
        pub = _pub_from_b64u(pub_b64u)
        pub.verify(_b64u_decode(sig_b64u), _canonical(payload))
        return True
    except (InvalidSignature, ValueError, Exception):
        return False


def make_connect_claim(priv_b64u: str, agent_id: str) -> str:
    """Build a signed connect token: base64url(json) + '.' + sig.

    The agent sends this as ``Authorization: Bearer <token>`` on its WebSocket.
    """
    payload = {"agent_id": agent_id, "iat": int(time.time()), "nonce": secrets.token_urlsafe(8)}
    return _b64u(_canonical(payload)) + "." + sign_payload(priv_b64u, payload)


def verify_connect_claim(token: str, pub_b64u: str) -> str | None:
    """Return the agent id if the connect claim is valid + fresh, else None."""
    try:
        body, sig = token.split(".", 1)
    except ValueError:
        return None
    try:
        payload = json.loads(_b64u_decode(body))
    except (ValueError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict) or "agent_id" not in payload or "iat" not in payload:
        return None
    if abs(int(time.time()) - int(payload["iat"])) > _CONNECT_TTL:
        return None
    if not verify_payload(pub_b64u, payload, sig):
        return None
    return payload["agent_id"]

