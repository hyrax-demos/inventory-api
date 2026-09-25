"""Unit tests for the stock cache key helpers in ``app.cache``."""

from app import cache


def test_stock_cache_key_includes_tenant_warehouse_and_sku():
    key = cache.stock_cache_key("tenant-a", "w1", "WIDGET")
    assert key == "stock:v2:tenant-a:WIDGET:w1"


def test_stock_cache_key_distinguishes_each_component():
    base = cache.stock_cache_key("tenant-a", "w1", "WIDGET")
    assert cache.stock_cache_key("tenant-b", "w1", "WIDGET") != base
    assert cache.stock_cache_key("tenant-a", "w2", "WIDGET") != base
    assert cache.stock_cache_key("tenant-a", "w1", "GADGET") != base
    # Case-sensitive, matching the SQL equality lookup behind the cache.
    assert cache.stock_cache_key("tenant-a", "w1", "widget") != base


def test_stock_cache_key_escapes_delimiters():
    assert cache.stock_cache_key("a:b", "w", "c") != cache.stock_cache_key(
        "a", "w", "b:c"
    )
    assert cache.stock_cache_key("t", "w1", "A:B") != cache.stock_cache_key(
        "t", "B", "A:w1"
    )


def test_stock_cache_key_is_distinct_from_legacy_and_price_keys():
    key = cache.stock_cache_key("t", "w1", "WIDGET")
    assert key != cache.stock_key("WIDGET")
    assert key != cache.price_key("WIDGET", "w1")


def test_invalidate_stock_drops_only_the_exact_key():
    target = cache.stock_cache_key("t", "w1", "WIDGET")
    other_wh = cache.stock_cache_key("t", "w2", "WIDGET")
    other_tenant = cache.stock_cache_key("u", "w1", "WIDGET")
    for k in (target, other_wh, other_tenant):
        cache.put(k, 1)

    cache.invalidate_stock("t", "w1", "WIDGET")

    assert cache.get(target) is None
    assert cache.get(other_wh) == 1
    assert cache.get(other_tenant) == 1


def test_invalidate_stock_all_warehouses_is_tenant_and_sku_scoped():
    w1 = cache.stock_cache_key("t", "w1", "WIDGET")
    w2 = cache.stock_cache_key("t", "w2", "WIDGET")
    other_sku = cache.stock_cache_key("t", "w1", "WIDGETX")
    other_tenant = cache.stock_cache_key("u", "w1", "WIDGET")
    for k in (w1, w2, other_sku, other_tenant):
        cache.put(k, 1)

    cache.invalidate_stock_all_warehouses("t", "WIDGET")

    assert cache.get(w1) is None
    assert cache.get(w2) is None
    assert cache.get(other_sku) == 1
    assert cache.get(other_tenant) == 1
