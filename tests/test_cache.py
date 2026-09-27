"""Unit tests for the stock cache key helper.

GET /items/{sku}/stock (app/routes/items.py) looks up on-hand quantity
scoped by tenant_id + warehouse_id + sku, so the cache key must encode all
three. Every writer (reserve_stock, release_reservation, bulk_adjust) must
produce the identical string for the same (tenant, warehouse, sku) so an
invalidation actually clears what the GET handler reads -- and a different
tenant or warehouse for the same SKU must never collide with it.
"""

from app import cache


def test_stock_cache_key_matches_get_handler_format():
    assert (
        cache.stock_cache_key("tenant-a", "w1", sku="WIDGET")
        == "stock:tenant-a:w1:WIDGET"
    )


def test_stock_cache_key_varies_by_tenant():
    """Two tenants holding the same SKU in the same warehouse must not
    share a cache entry -- otherwise one tenant's stock read/invalidate
    would leak into or clobber the other's."""
    key_a = cache.stock_cache_key("tenant-a", "w1", sku="WIDGET")
    key_b = cache.stock_cache_key("tenant-b", "w1", sku="WIDGET")
    assert key_a != key_b


def test_stock_cache_key_varies_by_warehouse():
    """The same tenant and SKU in two different warehouses must also get
    distinct cache entries."""
    key_a = cache.stock_cache_key("tenant-a", "w1", sku="WIDGET")
    key_b = cache.stock_cache_key("tenant-a", "w2", sku="WIDGET")
    assert key_a != key_b


def test_stock_key_is_the_legacy_sku_only_format():
    """The legacy helper is intentionally narrower than stock_cache_key now
    that the latter is tenant+warehouse scoped -- it must not be used for
    any call site that has tenant_id/warehouse_id available."""
    assert cache.stock_key("WIDGET") == "stock:WIDGET"
    assert cache.stock_key("WIDGET") != cache.stock_cache_key(
        "tenant-a", "w1", sku="WIDGET"
    )
