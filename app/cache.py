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
    """Drop every entry whose key starts with ``prefix``."""
    for key in [k for k in _store if k.startswith(prefix)]:
        _store.pop(key, None)


def _part(value: str) -> str:
    # Percent-encode each key component (":" included) so values that contain
    # the separator can't collide with a different tenant/sku/warehouse tuple.
    return urllib.parse.quote(str(value), safe="")


def stock_sku_prefix(tenant_id: str, sku: str) -> str:
    """Key prefix shared by a tenant's cached stock for ``sku`` in every warehouse."""
    return f"stock:{_part(tenant_id)}:{_part(sku)}:"


def stock_key(tenant_id: str, sku: str, warehouse_id: str) -> str:
    """Cache key for a tenant's stock snapshot of a SKU at one warehouse."""
    return stock_sku_prefix(tenant_id, sku) + _part(warehouse_id)


def price_key(sku: str, warehouse_id: str) -> str:
    """Cache key for a SKU's price in a given warehouse."""
    return f"price:{warehouse_id}:{sku}"
