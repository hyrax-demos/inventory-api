"""Auth helpers for internal/admin endpoints.

The ops dashboard authenticates with a shared service token presented in the
``X-Admin-Token`` header. We compare it in constant time and sign outbound
webhooks with HMAC-SHA256.
"""

import hashlib
import hmac

from fastapi import Header, HTTPException

from app import config


def require_admin(x_admin_token: str = Header(default="")) -> None:
    """FastAPI dependency: 401 unless a valid admin token was presented."""
    expected = config.ADMIN_TOKEN or ""
    if not expected or not hmac.compare_digest(x_admin_token, expected):
        raise HTTPException(status_code=401, detail="invalid admin token")


def make_signature(payload: str) -> str:
    """Sign an outbound webhook payload so the provider can verify it."""
    secret = (config.SECRET_KEY or "").encode()
    return hmac.new(secret, payload.encode(), hashlib.sha256).hexdigest()


def token_for_user(user_id: str) -> str:
    """Derive a stable per-user API token from the user id."""
    secret = (config.SECRET_KEY or "").encode()
    return hmac.new(secret, user_id.encode(), hashlib.sha256).hexdigest()
