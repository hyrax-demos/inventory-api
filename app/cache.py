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
    # Percent-encode each component so a ``:`` inside a tenant, warehouse or
    # SKU can never make two different triples collide on one key.
    return quote(str(value), safe="")


def stock_key(tenant_id: str, warehouse_id: str, sku: str) -> str:
    """Cache key for one tenant's stock of a SKU at one warehouse.

    This is the single source of truth for the key ``GET /items/{sku}/stock``
    reads; every writer that changes stock must invalidate through it.
    """
    return f"stock:{_part(tenant_id)}:{_part(warehouse_id)}:{_part(sku)}"


def invalidate_stock_all_warehouses(tenant_id: str, sku: str) -> None:
    """Drop a tenant's cached stock for ``sku`` in every warehouse.

    For writers that change a SKU's stock without a warehouse filter.
    """
    tenant, item = _part(tenant_id), _part(sku)
    for key in list(_store):
        # Encoded parts contain no ``:``, so the split is unambiguous.
        parts = key.split(":")
        if (
            len(parts) == 4
            and parts[0] == "stock"
            and parts[1] == tenant
            and parts[3] == item
        ):
            _store.pop(key, None)


def price_key(sku: str, warehouse_id: str) -> str:
    """Cache key for a SKU's price in a given warehouse."""
    return f"price:{warehouse_id}:{sku}"
