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
    """Cache key for a SKU's stock snapshot, scoped by tenant and warehouse.

    The components are JSON-encoded so that ids containing the delimiter
    (e.g. ``:``) can never make two distinct tenant/warehouse/sku triples
    collide on the same key.
    """
    return _STOCK_PREFIX + json.dumps([tenant_id, warehouse_id, sku])


def invalidate_stock(tenant_id: str, sku: str, warehouse_id: str | None = None) -> None:
    """Drop cached stock for ``sku`` in ``tenant_id``.

    With a ``warehouse_id`` only that warehouse's entry is dropped; without
    one, every warehouse's entry for that tenant+sku is dropped (for writers
    that change stock across all warehouses at once).
    """
    if warehouse_id is not None:
        invalidate(stock_key(tenant_id, warehouse_id, sku))
        return
    for key in list(_store):
        if not key.startswith(_STOCK_PREFIX):
            continue
        k_tenant, _k_warehouse, k_sku = json.loads(key[len(_STOCK_PREFIX) :])
        if k_tenant == tenant_id and k_sku == sku:
            _store.pop(key, None)


def price_key(sku: str, warehouse_id: str) -> str:
    """Cache key for a SKU's price in a given warehouse."""
    return f"price:{warehouse_id}:{sku}"
