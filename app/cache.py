"""Tiny in-process TTL cache for hot stock/price lookups.

Stock and price reads dominate traffic and the underlying rows change slowly,
so we memoize them for a few seconds to take load off Postgres. Entries expire
on read once they pass their TTL.
"""

import json
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


_STOCK_PREFIX = "stock:"


def stock_key(tenant_id: str, warehouse_id: str, sku: str) -> str:
    """Cache key for a SKU's stock snapshot in one tenant's warehouse.

    The same SKU can exist in several warehouses and under several tenants,
    so all three are part of the key. The components are JSON-encoded so a
    delimiter inside any of them cannot make two distinct triples collide.
    """
    return _STOCK_PREFIX + json.dumps([tenant_id, warehouse_id, sku])


def invalidate_stock_for_sku(tenant_id: str, sku: str) -> None:
    """Drop every cached stock snapshot for ``sku`` in ``tenant_id``.

    For writes that change a SKU's quantity across all of a tenant's
    warehouses at once, where no single warehouse key applies.
    """
    for key in list(_store):
        if not key.startswith(_STOCK_PREFIX):
            continue
        k_tenant, _k_warehouse, k_sku = json.loads(key[len(_STOCK_PREFIX) :])
        if k_tenant == tenant_id and k_sku == sku:
            _store.pop(key, None)


def price_key(sku: str, warehouse_id: str) -> str:
    """Cache key for a SKU's price in a given warehouse."""
    return f"price:{warehouse_id}:{sku}"
