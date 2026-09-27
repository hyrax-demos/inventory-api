"""Tiny in-process TTL cache for hot stock/price lookups.

Stock and price reads dominate traffic and the underlying rows change slowly,
so we memoize them for a few seconds to take load off Postgres. Entries expire
on read once they pass their TTL.
"""

import time

# key -> (expires_at_monotonic, value)
_store: dict[str, tuple[float, object]] = {}

DEFAULT_TTL = 5.0


def _now() -> float:
    return time.monotonic()


def get(key: str):
    entry = _store.get(key)
    if entry is None:
        return None
    expires_at, value = entry
    if _now() >= expires_at:
        _store.pop(key, None)
        return None
    return value


def put(key: str, value, ttl: float = DEFAULT_TTL) -> None:
    _store[key] = (_now() + ttl, value)


def invalidate(key: str) -> None:
    _store.pop(key, None)


def stock_key(sku: str) -> str:
    """Legacy SKU-only cache key.

    Retained only for the unit test that pins its literal format. Do not
    use this for new call sites: it collides across tenants and
    warehouses because it drops both from the key, which is exactly the
    bug ``stock_cache_key`` below exists to avoid. Every caller that has
    tenant_id / warehouse_id available -- which is every current caller --
    must go through ``stock_cache_key`` instead.
    """
    return f"stock:{sku}"


def stock_cache_key(tenant_id: str, warehouse_id: str, *, sku: str) -> str:
    """Single source of truth for the stock-snapshot cache key.

    GET /items/{sku}/stock (app/routes/items.py) looks up on-hand quantity
    scoped by tenant_id + warehouse_id + sku, so the cache key must encode
    all three -- otherwise two different (tenant, warehouse) rows sharing
    a SKU would read and invalidate each other's cached quantity. Every
    module that reads or invalidates the stock cache (app/routes/items.py,
    app/routes/sync.py, app/routes/admin.py) must call this instead of
    formatting the string itself, so they can never drift apart.
    """
    return f"stock:{tenant_id}:{warehouse_id}:{sku}"


def price_key(sku: str, warehouse_id: str) -> str:
    """Cache key for a SKU's price in a given warehouse."""
    return f"price:{warehouse_id}:{sku}"
