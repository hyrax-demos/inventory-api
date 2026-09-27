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


def _key_part(value: str) -> str:
    # Percent-encode so an id containing ':' cannot collide with another key.
    return urllib.parse.quote(str(value), safe="")


def _stock_prefix(tenant_id: str, sku: str) -> str:
    return f"stock:{_key_part(tenant_id)}:{_key_part(sku)}:"


def stock_key(tenant_id: str, warehouse_id: str, sku: str) -> str:
    """Cache key for one tenant's stock of a SKU at one warehouse.

    This is the single source of truth for the key GET /items/{sku}/stock
    reads; every writer that changes on-hand quantity must invalidate through
    this helper (or ``invalidate_stock``) so reader and writers cannot drift.
    """
    return _stock_prefix(tenant_id, sku) + _key_part(warehouse_id)


def invalidate_stock(tenant_id: str, sku: str, warehouse_id: str | None = None) -> None:
    """Drop cached stock for a tenant's SKU.

    With ``warehouse_id`` only that warehouse's entry is dropped; without it,
    the SKU's entries across all of the tenant's warehouses are dropped (for
    writes that are not warehouse-scoped).
    """
    if warehouse_id is not None:
        invalidate(stock_key(tenant_id, warehouse_id, sku))
        return
    prefix = _stock_prefix(tenant_id, sku)
    for key in [k for k in _store if k.startswith(prefix)]:
        _store.pop(key, None)


def price_key(sku: str, warehouse_id: str) -> str:
    """Cache key for a SKU's price in a given warehouse."""
    return f"price:{warehouse_id}:{sku}"
