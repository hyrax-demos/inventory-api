TENANT_A = {"X-Tenant-Id": "tenant-a"}
TENANT_B = {"X-Tenant-Id": "tenant-b"}


def _get_stock(client, sku, warehouse_id, headers):
    return client.get(
        f"/items/{sku}/stock", params={"warehouse_id": warehouse_id}, headers=headers
    )


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


def test_get_stock_cache_does_not_leak_across_warehouses(client, fake_db):
    """The same SKU in two warehouses must be cached (and read) separately."""
    fake_db.add_item(sku="WIDGET", warehouse_id="w1", quantity=10, tenant_id="tenant-a")
    fake_db.add_item(sku="WIDGET", warehouse_id="w2", quantity=99, tenant_id="tenant-a")

    resp_w1 = _get_stock(client, "WIDGET", "w1", TENANT_A)
    assert resp_w1.status_code == 200
    assert resp_w1.json()["quantity"] == 10

    # Querying w2 right after must not return w1's cached quantity.
    resp_w2 = _get_stock(client, "WIDGET", "w2", TENANT_A)
    assert resp_w2.status_code == 200
    assert resp_w2.json()["quantity"] == 99


def test_get_stock_cache_does_not_leak_across_tenants(client, fake_db):
    """The same SKU for two tenants must be cached (and read) separately."""
    fake_db.add_item(sku="WIDGET", warehouse_id="w1", quantity=10, tenant_id="tenant-a")
    fake_db.add_item(sku="WIDGET", warehouse_id="w1", quantity=77, tenant_id="tenant-b")

    resp_a = _get_stock(client, "WIDGET", "w1", TENANT_A)
    assert resp_a.status_code == 200
    assert resp_a.json()["quantity"] == 10

    # Querying tenant-b right after must not return tenant-a's cached quantity.
    resp_b = _get_stock(client, "WIDGET", "w1", TENANT_B)
    assert resp_b.status_code == 200
    assert resp_b.json()["quantity"] == 77


def test_reserve_stock_invalidates_cache_for_reserved_warehouse_only(client, fake_db):
    """A reservation must be reflected on the next read for its own
    warehouse, without disturbing a different warehouse's cached value."""
    fake_db.add_item(sku="WIDGET", warehouse_id="w1", quantity=10, tenant_id="tenant-a")
    fake_db.add_item(sku="WIDGET", warehouse_id="w2", quantity=50, tenant_id="tenant-a")

    # Warm the cache for both warehouses.
    assert _get_stock(client, "WIDGET", "w1", TENANT_A).json()["quantity"] == 10
    assert _get_stock(client, "WIDGET", "w2", TENANT_A).json()["quantity"] == 50

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

    # w1's next read must reflect the reservation, not the stale cached value.
    after_w1 = _get_stock(client, "WIDGET", "w1", TENANT_A)
    assert after_w1.json()["quantity"] == 7

    # w2's cached value is untouched by a reservation against w1.
    after_w2 = _get_stock(client, "WIDGET", "w2", TENANT_A)
    assert after_w2.json()["quantity"] == 50
