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


def test_release_reservation_invalidates_the_read_cache(client, fake_db):
    """A release must be reflected on the very next stock read for that
    tenant+warehouse+sku, even though the stock was cached beforehand."""
    fake_db.add_item(sku="WIDGET", warehouse_id="w1", quantity=5, tenant_id="tenant-a")
    fake_db.add_reservation(
        order_id="order-1",
        tenant_id="tenant-a",
        sku="WIDGET",
        warehouse_id="w1",
        quantity=3,
    )

    # Warm the stock cache before releasing.
    warm = client.get(
        "/items/WIDGET/stock",
        params={"warehouse_id": "w1"},
        headers={"X-Tenant-Id": "tenant-a"},
    )
    assert warm.json()["quantity"] == 5

    resp = client.post(
        "/reservations/order-1/release", headers={"X-Tenant-Id": "tenant-a"}
    )
    assert resp.status_code == 200
    assert resp.json()["released"] == 3

    after = client.get(
        "/items/WIDGET/stock",
        params={"warehouse_id": "w1"},
        headers={"X-Tenant-Id": "tenant-a"},
    )
    assert after.json()["quantity"] == 8


def test_release_reservation_does_not_invalidate_a_different_warehouse(client, fake_db):
    """Releasing a reservation in one warehouse must not evict the cached
    stock of the same SKU in a different warehouse."""
    fake_db.add_item(sku="WIDGET", warehouse_id="w1", quantity=5, tenant_id="tenant-a")
    fake_db.add_item(sku="WIDGET", warehouse_id="w2", quantity=20, tenant_id="tenant-a")
    fake_db.add_reservation(
        order_id="order-1",
        tenant_id="tenant-a",
        sku="WIDGET",
        warehouse_id="w1",
        quantity=3,
    )

    warm = client.get(
        "/items/WIDGET/stock",
        params={"warehouse_id": "w2"},
        headers={"X-Tenant-Id": "tenant-a"},
    )
    assert warm.json()["quantity"] == 20

    resp = client.post(
        "/reservations/order-1/release", headers={"X-Tenant-Id": "tenant-a"}
    )
    assert resp.status_code == 200

    still_cached = client.get(
        "/items/WIDGET/stock",
        params={"warehouse_id": "w2"},
        headers={"X-Tenant-Id": "tenant-a"},
    )
    assert still_cached.json()["quantity"] == 20
