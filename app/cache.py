"""Tiny in-process TTL cache for hot stock/price lookups.

Stock and price reads dominate traffic and the underlying rows change slowly,
so we memoize them for a few seconds to take load off Postgres. Entries expire
on read once they pass their TTL.
"""
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


def invalidate_prefix(prefix: str) -> None:
    """Drop every cached entry whose key starts with ``prefix``.

    Used when a write affects a SKU but the specific warehouse it landed in
    isn't known at the call site (e.g. a bulk adjustment keyed only by SKU),
    so every warehouse-scoped entry for that SKU must be dropped instead of
    a single key.
    """
    for key in [k for k in _store if k.startswith(prefix)]:
        _store.pop(key, None)


def stock_key(tenant_id: str, sku: str, warehouse_id: str) -> str:
    """Cache key for a SKU's stock snapshot, scoped to tenant and warehouse.

    Stock is looked up per (tenant, sku, warehouse): two tenants -- or two
    warehouses within the same tenant -- can hold different quantities for
    the same SKU, so all three must be part of the key or one lookup's
    cached value leaks into the others. ``warehouse_id`` is last so
    ``stock_key_prefix`` can invalidate every warehouse for a tenant+SKU
    without also matching unrelated SKUs.
    """
    return f"stock:{tenant_id}:{sku}:{warehouse_id}"


def stock_key_prefix(tenant_id: str, sku: str) -> str:
    """Prefix matching every warehouse-scoped stock key for a tenant+SKU.

    Used with ``invalidate_prefix`` when a write touches a SKU across
    warehouses we can't individually enumerate at the call site.
    """
    return f"stock:{tenant_id}:{sku}:"


def price_key(sku: str, warehouse_id: str) -> str:
    """Cache key for a SKU's price in a given warehouse."""
    return f"price:{warehouse_id}:{sku}"
