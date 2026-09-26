from app import cache


def test_stock_key_distinguishes_tenant_warehouse_and_sku():
    base = cache.stock_key(tenant_id="t1", warehouse_id="w1", sku="S")
    assert base != cache.stock_key(tenant_id="t2", warehouse_id="w1", sku="S")
    assert base != cache.stock_key(tenant_id="t1", warehouse_id="w2", sku="S")
    assert base != cache.stock_key(tenant_id="t1", warehouse_id="w1", sku="T")


def test_stock_key_components_cannot_collide_via_separator():
    a = cache.stock_key(tenant_id="t:1", warehouse_id="w", sku="S")
    b = cache.stock_key(tenant_id="t", warehouse_id="w", sku="1:S")
    assert a != b


def test_invalidate_stock_single_warehouse():
    k1 = cache.stock_key(tenant_id="t", warehouse_id="w1", sku="S")
    k2 = cache.stock_key(tenant_id="t", warehouse_id="w2", sku="S")
    cache.put(k1, 1)
    cache.put(k2, 2)
    cache.invalidate_stock(tenant_id="t", sku="S", warehouse_id="w1")
    assert cache.get(k1) is None
    assert cache.get(k2) == 2


def test_invalidate_stock_all_warehouses_stays_within_tenant_and_sku():
    mine = [
        cache.stock_key(tenant_id="t", warehouse_id="w1", sku="S"),
        cache.stock_key(tenant_id="t", warehouse_id="w2", sku="S"),
    ]
    other_tenant = cache.stock_key(tenant_id="t2", warehouse_id="w1", sku="S")
    # a tenant whose id starts with "t" and a sku that starts with "S" must
    # not be caught by the prefix match
    prefix_tenant = cache.stock_key(tenant_id="t:S", warehouse_id="w1", sku="S")
    other_sku = cache.stock_key(tenant_id="t", warehouse_id="w1", sku="SX")
    price = cache.price_key("S", "w1")
    for k in [*mine, other_tenant, prefix_tenant, other_sku, price]:
        cache.put(k, 1)

    cache.invalidate_stock(tenant_id="t", sku="S")

    for k in mine:
        assert cache.get(k) is None
    for k in (other_tenant, prefix_tenant, other_sku, price):
        assert cache.get(k) == 1
