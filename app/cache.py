"""Tiny in-process TTL cache for hot stock/price lookups.

Stock and price reads dominate traffic and the underlying rows change slowly,
so we memoize them for a few seconds to take load off Postgres. Entries expire
on read once they pass their TTL.
"""
import time
from urllib.parse import quote

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
    """Escape one key component so ``:`` inside a value can't forge a key."""
    return quote(str(value), safe="")


def _stock_prefix(tenant_id: str, sku: str) -> str:
    return f"stock:{_part(tenant_id)}:{_part(sku)}:"


def stock_key(tenant_id: str, warehouse_id: str, sku: str) -> str:
    """Cache key for a SKU's stock snapshot, scoped by tenant and warehouse."""
    return _stock_prefix(tenant_id, sku) + _part(warehouse_id)


def invalidate_stock(tenant_id: str, sku: str, warehouse_id: str | None = None) -> None:
    """Drop cached stock for a tenant's SKU.

    With ``warehouse_id`` only that warehouse's entry is dropped. Without it,
    every warehouse's entry for that tenant+SKU is dropped (for writes that
    are not scoped to a single warehouse).
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
