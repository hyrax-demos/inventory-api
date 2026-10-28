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


def test_get_stock_cache_is_scoped_by_warehouse(client, fake_db):
    """One tenant holds the same SKU in two warehouses with different
    quantities; reading w1 first must not make w2 return w1's cached value."""
    fake_db.add_item(sku="WIDGET", warehouse_id="w1", quantity=5, tenant_id="tenant-a")
    fake_db.add_item(sku="WIDGET", warehouse_id="w2", quantity=50, tenant_id="tenant-a")
    first = client.get(
        "/items/WIDGET/stock", params={"warehouse_id": "w1"}, headers=TENANT_A
    )
    second = client.get(
        "/items/WIDGET/stock", params={"warehouse_id": "w2"}, headers=TENANT_A
    )
    assert first.json()["quantity"] == 5
    assert second.status_code == 200
    assert second.json()["quantity"] == 50


def test_get_stock_cache_is_scoped_by_tenant(client, fake_db):
    """Two tenants hold the same SKU in a same-named warehouse; tenant B must
    see its own quantity after tenant A's read has been cached, and a tenant
    with no such item must get 404 rather than A's cached value."""
    fake_db.add_item(sku="WIDGET", warehouse_id="w1", quantity=5, tenant_id="tenant-a")
    fake_db.add_item(sku="WIDGET", warehouse_id="w1", quantity=77, tenant_id="tenant-b")
    a = client.get(
        "/items/WIDGET/stock", params={"warehouse_id": "w1"}, headers=TENANT_A
    )
    b = client.get(
        "/items/WIDGET/stock", params={"warehouse_id": "w1"}, headers=TENANT_B
    )
    c = client.get(
        "/items/WIDGET/stock",
        params={"warehouse_id": "w1"},
        headers={"X-Tenant-Id": "tenant-c"},
    )
    assert a.json()["quantity"] == 5
    assert b.json()["quantity"] == 77
    assert c.status_code == 404


def test_reserve_stock_invalidates_cached_stock(client, fake_db):
    """The same SKU is cached in two warehouses of one tenant, then stock is
    reserved in one of them; that warehouse's next read shows the decrement
    and the other warehouse keeps its own quantity."""
    fake_db.add_item(sku="WIDGET", warehouse_id="w1", quantity=10, tenant_id="tenant-a")
    fake_db.add_item(sku="WIDGET", warehouse_id="w2", quantity=20, tenant_id="tenant-a")

    def stock(wh):
        resp = client.get(
            "/items/WIDGET/stock", params={"warehouse_id": wh}, headers=TENANT_A
        )
        return resp.json()["quantity"]

    assert stock("w1") == 10
    assert stock("w2") == 20
    resp = client.post(
        "/items/reserve",
        json={
            "sku": "WIDGET",
            "warehouse_id": "w2",
            "quantity": 3,
            "order_id": "order-1",
        },
        headers=TENANT_A,
    )
    assert resp.status_code == 200
    assert stock("w2") == 17
    assert stock("w1") == 10
