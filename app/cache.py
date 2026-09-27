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
    # Percent-encode every key component (including ':') so ids containing the
    # delimiter can never make two different scopes produce the same key.
    return quote(str(value), safe="")


def _stock_sku_prefix(tenant_id: str, sku: str) -> str:
    return f"stock:{_part(tenant_id)}:{_part(sku)}:"


def stock_key(*, tenant_id: str, warehouse_id: str, sku: str) -> str:
    """Cache key for a SKU's stock snapshot, scoped to tenant and warehouse.

    The same SKU can exist in several warehouses and under several tenants,
    each with its own on-hand quantity, so all three are part of the key.
    """
    return _stock_sku_prefix(tenant_id, sku) + _part(warehouse_id)


def invalidate_stock_sku(tenant_id: str, sku: str) -> None:
    """Drop cached stock for a tenant's SKU in every warehouse.

    For writes that change a SKU's quantity without targeting one warehouse.
    """
    prefix = _stock_sku_prefix(tenant_id, sku)
    for key in [k for k in _store if k.startswith(prefix)]:
        _store.pop(key, None)


def price_key(sku: str, warehouse_id: str) -> str:
    """Cache key for a SKU's price in a given warehouse."""
    return f"price:{warehouse_id}:{sku}"
