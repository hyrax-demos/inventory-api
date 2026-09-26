TENANT_A = {"X-Tenant-Id": "tenant-a"}
TENANT_B = {"X-Tenant-Id": "tenant-b"}


def test_get_item_missing_tenant_header_is_rejected(client):
    resp = client.get("/items/WIDGET")
    assert resp.status_code in (400, 422)


def test_get_item_found(client, fake_db):
    fake_db.add_item(
        sku="WIDGET", name="Widget", warehouse_id="w1", quantity=5, tenant_id="tenant-a"
    )
    resp = client.get("/items/WIDGET", headers=TENANT_A)
    assert resp.status_code == 200
    body = resp.json()
    assert body["sku"] == "WIDGET"
    assert body["quantity"] == 5


def test_get_item_not_found(client, fake_db):
    resp = client.get("/items/NOPE", headers=TENANT_A)
    assert resp.status_code == 404


def test_search_items_returns_page(client, fake_db):
    for i in range(3):
        fake_db.add_item(
            sku=f"SKU{i}",
            name=f"item {i}",
            warehouse_id="w1",
            quantity=1,
            tenant_id="tenant-a",
        )
    resp = client.get("/items", headers=TENANT_A)
    assert resp.status_code == 200
    body = resp.json()
    assert len(body["items"]) == 3
    assert body["next_cursor"] is None


def test_search_items_paginates(client, fake_db):
    for i in range(5):
        fake_db.add_item(
            sku=f"SKU{i}",
            name=f"item {i}",
            warehouse_id="w1",
            quantity=1,
            tenant_id="tenant-a",
        )
    resp = client.get("/items", params={"limit": 2}, headers=TENANT_A)
    assert resp.status_code == 200
    body = resp.json()
    assert len(body["items"]) == 2
    assert body["next_cursor"] is not None

    resp2 = client.get(
        "/items", params={"limit": 2, "cursor": body["next_cursor"]}, headers=TENANT_A
    )
    assert resp2.status_code == 200
    body2 = resp2.json()
    assert len(body2["items"]) == 2
    # the cursor page should not repeat the first item
    first_page_skus = {i["sku"] for i in body["items"]}
    second_page_skus = {i["sku"] for i in body2["items"]}
    assert first_page_skus.isdisjoint(second_page_skus)


def test_search_items_rejects_limit_zero(client, fake_db):
    resp = client.get("/items", params={"limit": 0}, headers=TENANT_A)
    assert resp.status_code == 422


def test_search_items_rejects_limit_above_max(client, fake_db):
    resp = client.get("/items", params={"limit": 201}, headers=TENANT_A)
    assert resp.status_code == 422


def test_search_items_accepts_limit_bounds(client, fake_db):
    for limit in (1, 200):
        resp = client.get("/items", params={"limit": limit}, headers=TENANT_A)
        assert resp.status_code == 200


def test_search_items_filters_by_warehouse(client, fake_db):
    fake_db.add_item(
        sku="A", name="a", warehouse_id="w1", quantity=1, tenant_id="tenant-a"
    )
    fake_db.add_item(
        sku="B", name="b", warehouse_id="w2", quantity=1, tenant_id="tenant-a"
    )
    resp = client.get("/items", params={"warehouse_id": "w1"}, headers=TENANT_A)
    assert resp.status_code == 200
    body = resp.json()
    assert [i["sku"] for i in body["items"]] == ["A"]


def test_search_items_scoped_to_tenant(client, fake_db):
    fake_db.add_item(
        sku="A", name="a", warehouse_id="w1", quantity=1, tenant_id="tenant-a"
    )
    fake_db.add_item(
        sku="B", name="b", warehouse_id="w1", quantity=1, tenant_id="tenant-b"
    )
    resp = client.get("/items", headers=TENANT_A)
    assert resp.status_code == 200
    body = resp.json()
    assert [i["sku"] for i in body["items"]] == ["A"]


def test_get_stock_returns_quantity(client, fake_db):
    fake_db.add_item(sku="WIDGET", warehouse_id="w1", quantity=42, tenant_id="tenant-a")
    resp = client.get(
        "/items/WIDGET/stock", params={"warehouse_id": "w1"}, headers=TENANT_A
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["sku"] == "WIDGET"
    assert body["quantity"] == 42


def test_get_stock_not_found(client, fake_db):
    resp = client.get(
        "/items/NOPE/stock", params={"warehouse_id": "w1"}, headers=TENANT_A
    )
    assert resp.status_code == 404


def test_reserve_stock_happy_path(client, fake_db):
    fake_db.add_item(sku="WIDGET", warehouse_id="w1", quantity=10, tenant_id="tenant-a")
    resp = client.post(
        "/items/reserve",
        json={
            "sku": "WIDGET",
            "warehouse_id": "w1",
            "quantity": 3,
            "order_id": "order-1",
        },
        headers=TENANT_A,
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "reserved"
    item = next(r for r in fake_db.items if r["sku"] == "WIDGET")
    assert item["quantity"] == 7
    assert len(fake_db.reservations) == 1


def test_reserve_stock_repeat_order_is_a_noop(client, fake_db):
    fake_db.add_item(sku="WIDGET", warehouse_id="w1", quantity=10, tenant_id="tenant-a")
    body = {"sku": "WIDGET", "warehouse_id": "w1", "quantity": 3, "order_id": "order-1"}
    first = client.post("/items/reserve", json=body, headers=TENANT_A)
    second = client.post("/items/reserve", json=body, headers=TENANT_A)
    assert first.status_code == 200
    assert second.status_code == 200
    assert second.json()["status"] == "already_reserved"
    # only reserved once
    item = next(r for r in fake_db.items if r["sku"] == "WIDGET")
    assert item["quantity"] == 7


def test_reserve_stock_insufficient_quantity(client, fake_db):
    fake_db.add_item(sku="WIDGET", warehouse_id="w1", quantity=1, tenant_id="tenant-a")
    resp = client.post(
        "/items/reserve",
        json={
            "sku": "WIDGET",
            "warehouse_id": "w1",
            "quantity": 5,
            "order_id": "order-1",
        },
        headers=TENANT_A,
    )
    assert resp.status_code == 409


def test_reserve_stock_missing_item(client, fake_db):
    resp = client.post(
        "/items/reserve",
        json={
            "sku": "NOPE",
            "warehouse_id": "w1",
            "quantity": 1,
            "order_id": "order-1",
        },
        headers=TENANT_A,
    )
    assert resp.status_code == 404


def test_get_stock_cache_isolated_per_tenant(client, fake_db):
    fake_db.add_item(sku="WIDGET", warehouse_id="w1", quantity=10, tenant_id="tenant-a")
    fake_db.add_item(sku="WIDGET", warehouse_id="w1", quantity=99, tenant_id="tenant-b")
    params = {"warehouse_id": "w1"}
    a = client.get("/items/WIDGET/stock", params=params, headers=TENANT_A)
    b = client.get("/items/WIDGET/stock", params=params, headers=TENANT_B)
    assert a.json()["quantity"] == 10
    assert b.json()["quantity"] == 99
    # warm-cache reads stay isolated too
    assert (
        client.get("/items/WIDGET/stock", params=params, headers=TENANT_A).json()[
            "quantity"
        ]
        == 10
    )
    assert (
        client.get("/items/WIDGET/stock", params=params, headers=TENANT_B).json()[
            "quantity"
        ]
        == 99
    )


def test_get_stock_cache_isolated_per_warehouse(client, fake_db):
    fake_db.add_item(sku="WIDGET", warehouse_id="w1", quantity=5, tenant_id="tenant-a")
    fake_db.add_item(sku="WIDGET", warehouse_id="w2", quantity=50, tenant_id="tenant-a")
    w1 = client.get(
        "/items/WIDGET/stock", params={"warehouse_id": "w1"}, headers=TENANT_A
    )
    w2 = client.get(
        "/items/WIDGET/stock", params={"warehouse_id": "w2"}, headers=TENANT_A
    )
    assert w1.json()["quantity"] == 5
    assert w2.json()["quantity"] == 50


def test_reserve_stock_invalidates_only_its_own_cache_entry(client, fake_db):
    from app import cache

    fake_db.add_item(sku="WIDGET", warehouse_id="w1", quantity=10, tenant_id="tenant-a")
    fake_db.add_item(sku="WIDGET", warehouse_id="w2", quantity=20, tenant_id="tenant-a")
    fake_db.add_item(sku="WIDGET", warehouse_id="w1", quantity=30, tenant_id="tenant-b")
    for wh, hdr in (("w1", TENANT_A), ("w2", TENANT_A), ("w1", TENANT_B)):
        client.get("/items/WIDGET/stock", params={"warehouse_id": wh}, headers=hdr)

    resp = client.post(
        "/items/reserve",
        json={
            "sku": "WIDGET",
            "warehouse_id": "w1",
            "quantity": 3,
            "order_id": "order-1",
        },
        headers=TENANT_A,
    )
    assert resp.status_code == 200

    assert cache.get(cache.stock_key("tenant-a", "w1", "WIDGET")) is None
    assert cache.get(cache.stock_key("tenant-a", "w2", "WIDGET")) == 20
    assert cache.get(cache.stock_key("tenant-b", "w1", "WIDGET")) == 30
    fresh = client.get(
        "/items/WIDGET/stock", params={"warehouse_id": "w1"}, headers=TENANT_A
    )
    assert fresh.json()["quantity"] == 7


def test_stock_key_components_cannot_collide():
    from app import cache

    assert cache.stock_key("a:b", "w", "s") != cache.stock_key("a", "b:w", "s")
    assert cache.stock_key("t", "w1", "s") != cache.stock_key("t", "w2", "s")
    assert cache.stock_key("t1", "w", "s") != cache.stock_key("t2", "w", "s")


def test_get_item_cache_hit_skips_db(client, fake_db):
    from app import cache

    fake_db.add_item(
        sku="WIDGET", warehouse_id="w1", quantity=5, price=2.5, tenant_id="tenant-a"
    )
    first = client.get("/items/WIDGET", headers=TENANT_A)
    assert first.status_code == 200
    assert cache.get(cache.item_key("tenant-a", "WIDGET"))["price"] == 2.5

    # Mutate the backing row out-of-band: a warm read must come from cache.
    fake_db.items[0]["price"] = 99.0
    second = client.get("/items/WIDGET", headers=TENANT_A)
    assert second.status_code == 200
    assert second.json() == first.json()


def test_get_item_cache_isolated_per_tenant(client, fake_db):
    fake_db.add_item(
        sku="WIDGET", warehouse_id="w1", quantity=5, price=1.0, tenant_id="tenant-a"
    )
    fake_db.add_item(
        sku="WIDGET", warehouse_id="w1", quantity=50, price=10.0, tenant_id="tenant-b"
    )
    a = client.get("/items/WIDGET", headers=TENANT_A).json()
    b = client.get("/items/WIDGET", headers=TENANT_B).json()
    assert (a["price"], a["quantity"]) == (1.0, 5)
    assert (b["price"], b["quantity"]) == (10.0, 50)
    # warm-cache reads stay isolated too
    assert client.get("/items/WIDGET", headers=TENANT_A).json()["price"] == 1.0
    assert client.get("/items/WIDGET", headers=TENANT_B).json()["price"] == 10.0


def test_get_item_cache_miss_for_other_tenant_is_404(client, fake_db):
    fake_db.add_item(sku="WIDGET", warehouse_id="w1", quantity=5, tenant_id="tenant-a")
    assert client.get("/items/WIDGET", headers=TENANT_A).status_code == 200
    assert client.get("/items/WIDGET", headers=TENANT_B).status_code == 404


def test_reserve_stock_invalidates_item_cache(client, fake_db):
    from app import cache

    fake_db.add_item(sku="WIDGET", warehouse_id="w1", quantity=10, tenant_id="tenant-a")
    fake_db.add_item(sku="WIDGET", warehouse_id="w1", quantity=30, tenant_id="tenant-b")
    assert client.get("/items/WIDGET", headers=TENANT_A).json()["quantity"] == 10
    assert client.get("/items/WIDGET", headers=TENANT_B).json()["quantity"] == 30

    resp = client.post(
        "/items/reserve",
        json={
            "sku": "WIDGET",
            "warehouse_id": "w1",
            "quantity": 3,
            "order_id": "order-1",
        },
        headers=TENANT_A,
    )
    assert resp.status_code == 200

    assert cache.get(cache.item_key("tenant-a", "WIDGET")) is None
    assert cache.get(cache.item_key("tenant-b", "WIDGET"))["quantity"] == 30
    assert client.get("/items/WIDGET", headers=TENANT_A).json()["quantity"] == 7


def test_item_key_is_tenant_scoped_and_collision_free():
    from app import cache

    assert cache.item_key("t1", "s") != cache.item_key("t2", "s")
    assert cache.item_key("a:b", "c") != cache.item_key("a", "b:c")
