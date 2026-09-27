"""Unit tests for app/cache.py's stock-key scoping and prefix invalidation."""

from app import cache as cache_module


def test_stock_key_differs_by_warehouse():
    key_w1 = cache_module.stock_key("tenant-a", "WIDGET", "w1")
    key_w2 = cache_module.stock_key("tenant-a", "WIDGET", "w2")
    assert key_w1 != key_w2


def test_stock_key_differs_by_tenant():
    key_a = cache_module.stock_key("tenant-a", "WIDGET", "w1")
    key_b = cache_module.stock_key("tenant-b", "WIDGET", "w1")
    assert key_a != key_b


def test_stock_key_differs_by_sku():
    key_widget = cache_module.stock_key("tenant-a", "WIDGET", "w1")
    key_gadget = cache_module.stock_key("tenant-a", "GADGET", "w1")
    assert key_widget != key_gadget


def test_invalidate_prefix_drops_only_matching_warehouse_entries():
    cache_module._store.clear()
    key_w1 = cache_module.stock_key("tenant-a", "WIDGET", "w1")
    key_w2 = cache_module.stock_key("tenant-a", "WIDGET", "w2")
    other_sku_key = cache_module.stock_key("tenant-a", "GADGET", "w1")
    other_tenant_key = cache_module.stock_key("tenant-b", "WIDGET", "w1")

    cache_module.put(key_w1, 1)
    cache_module.put(key_w2, 2)
    cache_module.put(other_sku_key, 3)
    cache_module.put(other_tenant_key, 4)

    cache_module.invalidate_prefix(cache_module.stock_key_prefix("tenant-a", "WIDGET"))

    assert cache_module.get(key_w1) is None
    assert cache_module.get(key_w2) is None
    # Unrelated SKU/tenant entries survive the prefix invalidation.
    assert cache_module.get(other_sku_key) == 3
    assert cache_module.get(other_tenant_key) == 4

    cache_module._store.clear()
