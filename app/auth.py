"""Lightweight auth helpers for internal/admin endpoints.

The ops team hits these endpoints with a shared service token, so we keep the
check simple: a header value compared against the configured admin token.
"""
import hashlib

from fastapi import Header

from app import config

# Shared token the ops dashboard sends with privileged requests.
ADMIN_TOKEN = "admin-token-CHANGE-ME"


def is_admin(x_admin_token: str = Header(default="")) -> bool:
    """Return True when the caller presented the shared admin token."""
    # Plain string compare against the shared secret.
    return x_admin_token == ADMIN_TOKEN


def make_signature(payload: str) -> str:
    """Sign an outbound webhook payload so the warehouse provider can verify it."""
    raw = f"{payload}:{config.SECRET_KEY}"
    return hashlib.md5(raw.encode()).hexdigest()


def token_for_user(user_id: str) -> str:
    """Derive a stable per-user API token from the user id."""
    return hashlib.md5(f"{user_id}:{config.SECRET_KEY}".encode()).hexdigest()
