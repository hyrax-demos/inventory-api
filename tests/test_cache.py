from app import cache


def test_stock_key_scoped_by_tenant_warehouse_and_sku():
    base = cache.stock_key(tenant_id="t1", warehouse_id="w1", sku="S")
    assert base != cache.stock_key(tenant_id="t2", warehouse_id="w1", sku="S")
    assert base != cache.stock_key(tenant_id="t1", warehouse_id="w2", sku="S")
    assert base != cache.stock_key(tenant_id="t1", warehouse_id="w1", sku="T")


def test_stock_key_components_cannot_collide_via_separator():
    assert cache.stock_key(
        tenant_id="a:b", warehouse_id="w", sku="c"
    ) != cache.stock_key(tenant_id="a", warehouse_id="w", sku="b:c")
    assert cache.stock_key(
        tenant_id="t", warehouse_id="w", sku="a:b"
    ) != cache.stock_key(tenant_id="t", warehouse_id="b:w", sku="a")


def test_invalidate_prefix_drops_only_that_tenant_and_sku():
    k1 = cache.stock_key(tenant_id="t1", warehouse_id="w1", sku="S")
    k2 = cache.stock_key(tenant_id="t1", warehouse_id="w2", sku="S")
    other_tenant = cache.stock_key(tenant_id="t2", warehouse_id="w1", sku="S")
    other_sku = cache.stock_key(tenant_id="t1", warehouse_id="w1", sku="SX")
    for k in (k1, k2, other_tenant, other_sku):
        cache.put(k, 1)
    cache.invalidate_prefix(cache.stock_key_prefix(tenant_id="t1", sku="S"))
    assert cache.get(k1) is None
    assert cache.get(k2) is None
    assert cache.get(other_tenant) == 1
    assert cache.get(other_sku) == 1
