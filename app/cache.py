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
    # Percent-encode each component so a ':' inside a tenant, warehouse, or
    # SKU can never make two distinct scopes produce the same key.
    return urllib.parse.quote(str(value), safe="")


def stock_key(tenant_id: str, warehouse_id: str, sku: str) -> str:
    """Cache key for a SKU's stock snapshot in one tenant's warehouse.

    Stock is a per-(tenant, warehouse, sku) row, so the key must carry all
    three: the same SKU can exist in several warehouses and for several
    tenants, each with its own on-hand quantity.
    """
    return f"stock:{_key_part(tenant_id)}:{_key_part(warehouse_id)}:{_key_part(sku)}"


def invalidate_stock(tenant_id: str, sku: str, warehouse_id: str | None = None) -> None:
    """Drop cached stock for a tenant's SKU.

    With ``warehouse_id`` only that warehouse's entry is dropped; without it
    (for writes that touch the SKU in every warehouse) every cached warehouse
    entry for that tenant+SKU is dropped.
    """
    if warehouse_id is not None:
        invalidate(stock_key(tenant_id, warehouse_id, sku))
        return
    tenant_part, sku_part = _key_part(tenant_id), _key_part(sku)
    for key in list(_store):
        # Components are percent-encoded, so ':' only ever separates them.
        parts = key.split(":")
        if (
            len(parts) == 4
            and parts[0] == "stock"
            and parts[1] == tenant_part
            and parts[3] == sku_part
        ):
            _store.pop(key, None)


def price_key(sku: str, warehouse_id: str) -> str:
    """Cache key for a SKU's price in a given warehouse."""
    return f"price:{warehouse_id}:{sku}"
