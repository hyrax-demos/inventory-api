import os

from app.routes import items as items_routes

TENANT_A = {"X-Tenant-Id": "tenant-a", "X-Admin-Token": os.environ["ADMIN_TOKEN"]}


def test_sync_prices_requires_admin_token(client, fake_db):
    # No network call is reached: the admin dependency runs before the body.
    resp = client.post("/sync/prices")
    assert resp.status_code == 401


def test_sync_single_item_happy_path(client, fake_db):
    fake_db.add_item(
        sku="WIDGET", warehouse_id="w1", quantity=1, price=1.0, tenant_id="tenant-a"
    )
    resp = client.post(
        "/sync/item/WIDGET",
        params={"warehouse_id": "w1", "price": 9.99},
        headers=TENANT_A,
    )
    assert resp.status_code == 200
    assert fake_db.items[0]["price"] == 9.99


def test_sync_single_item_not_found(client, fake_db):
    resp = client.post(
        "/sync/item/NOPE",
        params={"warehouse_id": "w1", "price": 1.0},
        headers=TENANT_A,
    )
    assert resp.status_code == 404


def test_release_reservation_happy_path(client, fake_db):
    fake_db.add_item(sku="WIDGET", warehouse_id="w1", quantity=5, tenant_id="tenant-a")
    fake_db.add_reservation(
        order_id="order-1",
        tenant_id="tenant-a",
        sku="WIDGET",
        warehouse_id="w1",
        quantity=3,
    )
    resp = client.post(
        "/reservations/order-1/release", headers={"X-Tenant-Id": "tenant-a"}
    )
    assert resp.status_code == 200
    assert resp.json()["released"] == 3
    assert fake_db.items[0]["quantity"] == 8


def test_release_reservation_not_found(client, fake_db):
    resp = client.post(
        "/reservations/does-not-exist/release", headers={"X-Tenant-Id": "tenant-a"}
    )
    assert resp.status_code == 404


def test_release_reservation_reflected_on_next_stock_read(client, fake_db):
    """A cached stock read for the reservation's tenant, warehouse and SKU is
    refreshed right after the reservation is released."""
    headers = {"X-Tenant-Id": "tenant-a"}
    params = {"warehouse_id": "w1"}
    fake_db.add_item(sku="WIDGET", warehouse_id="w1", quantity=5, tenant_id="tenant-a")
    fake_db.add_reservation(
        order_id="order-1",
        tenant_id="tenant-a",
        sku="WIDGET",
        warehouse_id="w1",
        quantity=3,
    )
    assert (
        client.get("/items/WIDGET/stock", params=params, headers=headers).json()[
            "quantity"
        ]
        == 5
    )
    resp = client.post("/reservations/order-1/release", headers=headers)
    assert resp.status_code == 200
    assert (
        client.get("/items/WIDGET/stock", params=params, headers=headers).json()[
            "quantity"
        ]
        == 8
    )


def test_release_reservation_refreshes_only_its_own_warehouse(client, fake_db):
    """The same SKU is stocked in two warehouses and both reads are cached; a
    release in w1 shows up on the next w1 read while w2 keeps its own value."""
    headers = {"X-Tenant-Id": "tenant-a"}
    params_w1 = {"warehouse_id": "w1"}
    params_w2 = {"warehouse_id": "w2"}
    fake_db.add_item(sku="WIDGET", warehouse_id="w1", quantity=5, tenant_id="tenant-a")
    fake_db.add_item(sku="WIDGET", warehouse_id="w2", quantity=2, tenant_id="tenant-a")
    fake_db.add_reservation(
        order_id="order-1",
        tenant_id="tenant-a",
        sku="WIDGET",
        warehouse_id="w1",
        quantity=3,
    )
    assert (
        client.get("/items/WIDGET/stock", params=params_w1, headers=headers).json()[
            "quantity"
        ]
        == 5
    )
    assert (
        client.get("/items/WIDGET/stock", params=params_w2, headers=headers).json()[
            "quantity"
        ]
        == 2
    )
    resp = client.post("/reservations/order-1/release", headers=headers)
    assert resp.status_code == 200
    assert (
        client.get("/items/WIDGET/stock", params=params_w1, headers=headers).json()[
            "quantity"
        ]
        == 8
    )
    assert (
        client.get("/items/WIDGET/stock", params=params_w2, headers=headers).json()[
            "quantity"
        ]
        == 2
    )


def test_release_reservation_uses_the_tenant_get_stock_derives(
    client, fake_db, monkeypatch
):
    """The header is sent as "Tenant-A" while _tenant lowercases it; the
    release must act on and invalidate the key for "tenant-a", the tenant
    get_stock reads under."""
    monkeypatch.setattr(items_routes, "_tenant", lambda value: value.lower())
    headers = {"X-Tenant-Id": "Tenant-A"}
    params = {"warehouse_id": "w1"}
    fake_db.add_item(sku="WIDGET", warehouse_id="w1", quantity=5, tenant_id="tenant-a")
    fake_db.add_reservation(
        order_id="order-1",
        tenant_id="tenant-a",
        sku="WIDGET",
        warehouse_id="w1",
        quantity=3,
    )
    resp = client.get("/items/WIDGET/stock", params=params, headers=headers)
    assert resp.json()["quantity"] == 5
    resp = client.post("/reservations/order-1/release", headers=headers)
    assert resp.status_code == 200
    resp = client.get("/items/WIDGET/stock", params=params, headers=headers)
    assert resp.json()["quantity"] == 8
