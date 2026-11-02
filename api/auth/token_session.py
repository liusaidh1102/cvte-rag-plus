"""Opaque token sessions backed by Redis.

The browser receives a random token. Only a SHA-256 digest of that token is
used as the Redis key, so a Redis dump cannot be replayed directly as a login
credential. Session metadata is stored as JSON and expires together with the
Redis key.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import secrets
import time
from typing import Any

TOKEN_PREFIX = "cvte-rag-plus:auth:session:"
DEFAULT_AUTH_TOKEN_TTL_SECONDS = 8 * 60 * 60


def auth_token_ttl_seconds() -> int:
    """Read the TTL lazily so dotenv is applied before the first login."""
    return int(os.environ.get("AUTH_TOKEN_TTL_SECONDS", str(DEFAULT_AUTH_TOKEN_TTL_SECONDS)))


def _redis():
    # Import lazily so importing the web app does not require Redis to be up.
    from rag.utils.redis_conn import REDIS_CONN

    return REDIS_CONN


def _session_key(token: str) -> str:
    digest = hashlib.sha256(token.encode("utf-8")).hexdigest()
    return f"{TOKEN_PREFIX}{digest}"


def extract_bearer_token(authorization: str | None) -> str | None:
    """Accept both ``Bearer <token>`` and the legacy raw-token header."""
    if not isinstance(authorization, str):
        return None
    parts = authorization.strip().split()
    if len(parts) == 2 and parts[0].lower() == "bearer":
        return parts[1]
    if len(parts) == 1 and parts[0] and parts[0].lower() != "bearer":
        return parts[0]
    return None


def create_token_session(user: dict[str, Any], ttl: int | None = None) -> tuple[str, int]:
    """Create a random token and persist its user claims in Redis."""
    expires_in = int(ttl or auth_token_ttl_seconds())
    if expires_in <= 0:
        raise ValueError("AUTH_TOKEN_TTL_SECONDS must be positive")

    token = secrets.token_urlsafe(32)
    now = int(time.time())
    payload = {
        "user_id": str(user["user_id"]),
        "username": user.get("username") or "",
        "email": user.get("email"),
        "role": user.get("role") or "user",
        "tenant_id": user.get("tenant_id"),
        "is_superuser": bool(user.get("is_superuser")),
        "login_channel": user.get("login_channel") or "password",
        "issued_at": now,
        "expires_at": now + expires_in,
    }
    if not _redis().set_obj(_session_key(token), payload, expires_in):
        raise RuntimeError("Redis is unavailable; cannot create login session")
    return token, expires_in


def get_token_session(token: str | None) -> dict[str, Any] | None:
    if not token:
        return None
    raw = _redis().get(_session_key(token))
    if not raw:
        return None
    try:
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8")
        payload = json.loads(raw)
    except (TypeError, ValueError, UnicodeDecodeError):
        logging.warning("Ignoring malformed auth session payload")
        return None
    if not isinstance(payload, dict) or not payload.get("user_id"):
        return None
    expires_at = payload.get("expires_at")
    if isinstance(expires_at, (int, float)) and expires_at <= time.time():
        delete_token_session(token)
        return None
    return payload


def delete_token_session(token: str | None) -> None:
    if not token:
        return
    _redis().delete(_session_key(token))
