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


def stock_key(sku: str) -> str:
    """Cache key for a SKU's stock snapshot.

    Kept for backwards compatibility -- prefer ``stock_cache_key`` below,
    which is the single source of truth for this format. The GET
    /items/{sku}/stock handler (app/routes/items.py) only ever keys its
    cache reads/writes by SKU today, so this delegates straight through
    rather than widening the key: any caller that has tenant_id /
    warehouse_id available should go through ``stock_cache_key`` instead
    so all call sites stay in lockstep if the format ever changes.
    """
    return stock_cache_key(sku=sku)


def stock_cache_key(
    tenant_id: str | None = None, warehouse_id: str | None = None, *, sku: str
) -> str:
    """Single source of truth for the stock-snapshot cache key.

    GET /items/{sku}/stock (app/routes/items.py) is the sole reader/writer
    of this key and only ever keys it by SKU -- ``tenant_id`` and
    ``warehouse_id`` are accepted here so every write site can pass the
    full identity of the row it just changed, but they are intentionally
    not part of the key today, matching the GET handler exactly. Every
    other module that invalidates the stock cache (app/routes/sync.py,
    app/routes/admin.py) must call this instead of formatting the string
    itself, so the GET handler and every writer can never drift apart.
    """
    return f"stock:{sku}"


def price_key(sku: str, warehouse_id: str) -> str:
    """Cache key for a SKU's price in a given warehouse."""
    return f"price:{warehouse_id}:{sku}"
