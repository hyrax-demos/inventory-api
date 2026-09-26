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


def _part(value: str) -> str:
    """Percent-encode one key component so ``:`` can't appear inside it.

    This keeps composite keys unambiguous: a tenant id with a ``:`` in it
    can't produce the same key as another tenant/sku pair, and a prefix
    match can't reach past its own component.
    """
    return urllib.parse.quote(str(value), safe="")


def _stock_prefix(tenant_id: str, sku: str) -> str:
    return f"stock:{_part(tenant_id)}:{_part(sku)}:"


def stock_key(*, tenant_id: str, warehouse_id: str, sku: str) -> str:
    """Cache key for one SKU's on-hand quantity at a warehouse for a tenant.

    Every part is required: the same SKU can exist in several warehouses and
    under several tenants, and each of those has its own quantity.
    """
    return _stock_prefix(tenant_id, sku) + _part(warehouse_id)


def invalidate_stock(
    *, tenant_id: str, sku: str, warehouse_id: str | None = None
) -> None:
    """Drop cached stock for ``sku`` under ``tenant_id``.

    With ``warehouse_id`` set, only that warehouse's entry is dropped. With
    ``warehouse_id=None``, the entry for every warehouse holding this SKU
    for this tenant is dropped (for writes not scoped to one warehouse).
    Other tenants' entries are never touched.
    """
    if warehouse_id is not None:
        invalidate(stock_key(tenant_id=tenant_id, warehouse_id=warehouse_id, sku=sku))
        return
    prefix = _stock_prefix(tenant_id, sku)
    # Snapshot the keys first: sync routes run in a threadpool, so the dict
    # can change size while we scan it.
    for key in list(_store):
        if key.startswith(prefix):
            _store.pop(key, None)


def price_key(sku: str, warehouse_id: str) -> str:
    """Cache key for a SKU's price in a given warehouse."""
    return f"price:{warehouse_id}:{sku}"
