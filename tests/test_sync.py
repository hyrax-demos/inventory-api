import os

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


def _stock(client, sku, warehouse_id, tenant_id):
    resp = client.get(
        f"/items/{sku}/stock",
        params={"warehouse_id": warehouse_id},
        headers={"X-Tenant-Id": tenant_id},
    )
    assert resp.status_code == 200
    return resp.json()["quantity"]


def test_release_reservation_is_reflected_on_next_stock_read(client, fake_db):
    fake_db.add_item(sku="WIDGET", warehouse_id="w1", quantity=5, tenant_id="tenant-a")
    fake_db.add_item(sku="WIDGET", warehouse_id="w2", quantity=50, tenant_id="tenant-a")
    fake_db.add_item(
        sku="WIDGET", warehouse_id="w1", quantity=500, tenant_id="tenant-b"
    )
    fake_db.add_reservation(
        order_id="order-1",
        tenant_id="tenant-a",
        sku="WIDGET",
        warehouse_id="w1",
        quantity=3,
    )
    assert _stock(client, "WIDGET", "w1", "tenant-a") == 5
    assert _stock(client, "WIDGET", "w2", "tenant-a") == 50
    assert _stock(client, "WIDGET", "w1", "tenant-b") == 500

    resp = client.post(
        "/reservations/order-1/release", headers={"X-Tenant-Id": "tenant-a"}
    )
    assert resp.status_code == 200

    assert _stock(client, "WIDGET", "w1", "tenant-a") == 8
    assert _stock(client, "WIDGET", "w2", "tenant-a") == 50
    assert _stock(client, "WIDGET", "w1", "tenant-b") == 500


def test_reserve_then_release_round_trips_through_stock_cache(client, fake_db):
    fake_db.add_item(sku="WIDGET", warehouse_id="w1", quantity=10, tenant_id="tenant-a")
    assert _stock(client, "WIDGET", "w1", "tenant-a") == 10
    resp = client.post(
        "/items/reserve",
        json={
            "sku": "WIDGET",
            "warehouse_id": "w1",
            "quantity": 4,
            "order_id": "order-1",
        },
        headers={"X-Tenant-Id": "tenant-a"},
    )
    assert resp.status_code == 200
    assert _stock(client, "WIDGET", "w1", "tenant-a") == 6
    resp = client.post(
        "/reservations/order-1/release", headers={"X-Tenant-Id": "tenant-a"}
    )
    assert resp.status_code == 200
    assert _stock(client, "WIDGET", "w1", "tenant-a") == 10
