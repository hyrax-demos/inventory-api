"""Configuration for the inventory API.

All sensitive values are read from the environment. The service refuses to
boot in production if required secrets are missing (see ``require``).
"""
import os


def _get(name: str, default: str | None = None) -> str | None:
    return os.environ.get(name, default)


def require(name: str) -> str:
    """Read a required environment variable or raise at import time."""
    value = os.environ.get(name)
    if value is None or value == "":
        raise RuntimeError(f"missing required environment variable: {name}")
    return value


# Database connection. Host/user/name carry local-dev defaults; the password
# is always required so we never ship a fallback credential.
DB_HOST = _get("DB_HOST", "db.internal.local")
DB_USER = _get("DB_USER", "inventory")
DB_NAME = _get("DB_NAME", "inventory")
DB_PASSWORD = _get("DB_PASSWORD")

# Secret used to sign internal service tokens and outbound webhooks. Required.
SECRET_KEY = _get("SECRET_KEY")

# Warehouse provider credential, read from the environment.
WAREHOUSE_API_KEY = _get("WAREHOUSE_API_KEY")

# Shared token the ops dashboard presents on admin endpoints.
ADMIN_TOKEN = _get("ADMIN_TOKEN")

# Allow-list of hosts the price-sync integration may talk to.
PROVIDER_ALLOWED_HOSTS = frozenset(
    h.strip()
    for h in _get(
        "PROVIDER_ALLOWED_HOSTS",
        "prices.warehouse-provider.example",
    ).split(",")
    if h.strip()
)
