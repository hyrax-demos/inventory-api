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


def _part(value: str) -> str:
    # Percent-encode so a ':' inside a component can't collide with the
    # separator (e.g. tenant "a:b" + warehouse "c" vs tenant "a" + "b:c").
    return urllib.parse.quote(str(value), safe="")


def stock_cache_key(tenant_id: str, warehouse_id: str, sku: str) -> str:
    """Cache key for one tenant's stock snapshot of a SKU at a warehouse.

    This is the ONLY place a stock cache key is built. GET
    /items/{sku}/stock reads it, and every writer that changes on-hand
    quantity invalidates it, so readers and writers can't disagree on the key.
    """
    return f"stock:{_part(tenant_id)}:{_part(warehouse_id)}:{_part(sku)}"


def invalidate_stock(tenant_id: str, warehouse_id: str, sku: str) -> None:
    """Drop the cached stock snapshot for tenant + warehouse + sku."""
    invalidate(stock_cache_key(tenant_id, warehouse_id, sku))


def invalidate_stock_all_warehouses(tenant_id: str, sku: str) -> None:
    """Drop a tenant's cached stock for a SKU in every warehouse.

    For writers that change a SKU's quantity without targeting a single
    warehouse.
    """
    prefix = f"stock:{_part(tenant_id)}:"
    suffix = f":{_part(sku)}"
    for key in [k for k in _store if k.startswith(prefix) and k.endswith(suffix)]:
        _store.pop(key, None)


def price_key(sku: str, warehouse_id: str) -> str:
    """Cache key for a SKU's price in a given warehouse."""
    return f"price:{warehouse_id}:{sku}"
