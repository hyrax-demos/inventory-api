from app import cache


def test_stock_key_distinguishes_tenant_warehouse_and_sku():
    keys = {
        cache.stock_key("t1", "w1", "S"),
        cache.stock_key("t2", "w1", "S"),
        cache.stock_key("t1", "w2", "S"),
        cache.stock_key("t1", "w1", "T"),
    }
    assert len(keys) == 4


def test_stock_key_components_cannot_collide_via_separator():
    assert cache.stock_key("t:w", "x", "S") != cache.stock_key("t", "w:x", "S")


def test_invalidate_stock_without_warehouse_is_tenant_and_sku_scoped():
    cache.put(cache.stock_key("t1", "w1", "S"), 1)
    cache.put(cache.stock_key("t1", "w2", "S"), 2)
    cache.put(cache.stock_key("t2", "w1", "S"), 3)
    cache.put(cache.stock_key("t1", "w1", "OTHER"), 4)
    cache.invalidate_stock("t1", "S")
    assert cache.get(cache.stock_key("t1", "w1", "S")) is None
    assert cache.get(cache.stock_key("t1", "w2", "S")) is None
    assert cache.get(cache.stock_key("t2", "w1", "S")) == 3
    assert cache.get(cache.stock_key("t1", "w1", "OTHER")) == 4


def test_invalidate_stock_with_warehouse_only_drops_that_warehouse():
    cache.put(cache.stock_key("t1", "w1", "S"), 1)
    cache.put(cache.stock_key("t1", "w2", "S"), 2)
    cache.invalidate_stock("t1", "S", "w1")
    assert cache.get(cache.stock_key("t1", "w1", "S")) is None
    assert cache.get(cache.stock_key("t1", "w2", "S")) == 2
