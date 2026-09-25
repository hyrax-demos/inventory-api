"""Tiny in-process TTL cache for hot stock/price lookups.

Stock and price reads dominate traffic and the underlying rows change slowly,
so we memoize them for a few seconds to take load off Postgres. Entries expire
on read once they pass their TTL.
"""

import time
import urllib.parse

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
    """Legacy, tenant/warehouse-agnostic stock key.

    Kept so older callers keep working. ``GET /items/{sku}/stock`` reads
    :func:`stock_cache_key` instead; the ``invalidate_stock*`` helpers drop
    this key too, so it can't hang around stale.
    """
    return f"stock:{sku}"


_STOCK_KEY_PREFIX = "stock:v2"


def _part(value: str) -> str:
    # Percent-encode every component, ":" included, so the delimiter never
    # shows up inside a component and ("a:b", "c") can't collide with
    # ("a", "b:c"). Values stay case-sensitive to match the SQL equality
    # lookups that fill the cache.
    return urllib.parse.quote(str(value), safe="")


def _stock_sku_prefix(tenant_id: str, sku: str) -> str:
    return f"{_STOCK_KEY_PREFIX}:{_part(tenant_id)}:{_part(sku)}:"


def stock_cache_key(tenant_id: str, warehouse_id: str, sku: str) -> str:
    """Cache key for a tenant's on-hand quantity of ``sku`` at ``warehouse_id``.

    ``GET /items/{sku}/stock`` reads this key. Every writer that changes the
    quantity must invalidate through the same helper so the two can't drift.
    """
    return _stock_sku_prefix(tenant_id, sku) + _part(warehouse_id)


def invalidate_stock(tenant_id: str, warehouse_id: str, sku: str) -> None:
    """Drop the cached stock for one tenant + warehouse + sku."""
    invalidate(stock_cache_key(tenant_id, warehouse_id, sku))
    invalidate(stock_key(sku))


def invalidate_stock_all_warehouses(tenant_id: str, sku: str) -> None:
    """Drop the cached stock for a tenant's ``sku`` in every warehouse.

    For writers that change a SKU's quantity without naming a warehouse.
    """
    prefix = _stock_sku_prefix(tenant_id, sku)
    for key in [k for k in _store if k.startswith(prefix)]:
        _store.pop(key, None)
    invalidate(stock_key(sku))


def price_key(sku: str, warehouse_id: str) -> str:
    """Cache key for a SKU's price in a given warehouse."""
    return f"price:{warehouse_id}:{sku}"
