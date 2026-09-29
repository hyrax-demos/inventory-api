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


def invalidate_prefix(prefix: str) -> None:
    for key in [k for k in _store if k.startswith(prefix)]:
        _store.pop(key, None)


def _part(value: str) -> str:
    # Percent-encode each component (":" included) so ids that contain the
    # separator can never collide with another tenant/sku/warehouse's key.
    return urllib.parse.quote(str(value), safe="")


def _stock_prefix(tenant_id: str, sku: str) -> str:
    return f"stock:{_part(tenant_id)}:{_part(sku)}:"


def stock_key(tenant_id: str, sku: str, warehouse_id: str) -> str:
    """Cache key for a SKU's on-hand quantity at one warehouse of one tenant.

    This is the only builder for stock keys: GET /items/{sku}/stock reads it
    and every stock-mutating path invalidates through it (or through
    ``invalidate_stock``), so the reader and invalidators cannot drift.
    """
    return _stock_prefix(tenant_id, sku) + _part(warehouse_id)


def invalidate_stock(tenant_id: str, sku: str, warehouse_id: str | None = None) -> None:
    """Drop cached stock for a tenant's SKU.

    With ``warehouse_id`` only that warehouse's entry is evicted; without it
    every warehouse's entry for the tenant+SKU is (for writes that are not
    warehouse-scoped).
    """
    if warehouse_id is None:
        invalidate_prefix(_stock_prefix(tenant_id, sku))
    else:
        invalidate(stock_key(tenant_id, sku, warehouse_id))


def price_key(sku: str, warehouse_id: str) -> str:
    """Cache key for a SKU's price in a given warehouse."""
    return f"price:{warehouse_id}:{sku}"
