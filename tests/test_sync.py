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


def _seed_reservation(fake_db):
    fake_db.add_item(sku="WIDGET", warehouse_id="w1", quantity=5, tenant_id="tenant-a")
    fake_db.add_reservation(
        order_id="order-1",
        tenant_id="tenant-a",
        sku="WIDGET",
        warehouse_id="w1",
        quantity=3,
    )


def _fail_on(fake_db, monkeypatch, prefix):
    real_execute = fake_db.execute

    def failing_execute(sql, params=()):
        if sql.startswith(prefix):
            raise RuntimeError("simulated DB failure")
        return real_execute(sql, params)

    monkeypatch.setattr(fake_db, "execute", failing_execute)


def test_release_reservation_removes_row_and_restores_stock(client, fake_db):
    _seed_reservation(fake_db)
    resp = client.post(
        "/reservations/order-1/release", headers={"X-Tenant-Id": "tenant-a"}
    )
    assert resp.status_code == 200
    assert resp.json() == {"order_id": "order-1", "released": 3}
    assert fake_db.reservations == []
    assert fake_db.items[0]["quantity"] == 8


def test_release_reservation_stock_update_failure_keeps_reservation(
    fake_db, monkeypatch
):
    from fastapi.testclient import TestClient

    from app import cache
    from app.main import app

    _seed_reservation(fake_db)
    cache.put(cache.stock_key("WIDGET"), {"quantity": 5})
    _fail_on(fake_db, monkeypatch, "UPDATE items SET quantity = quantity + %s")

    client = TestClient(app, raise_server_exceptions=False)
    resp = client.post(
        "/reservations/order-1/release", headers={"X-Tenant-Id": "tenant-a"}
    )

    assert resp.status_code == 500
    assert len(fake_db.reservations) == 1
    assert fake_db.reservations[0]["order_id"] == "order-1"
    assert fake_db.items[0]["quantity"] == 5
    # Rolled back: the cache must not have been invalidated.
    assert cache.get(cache.stock_key("WIDGET")) == {"quantity": 5}


def test_release_reservation_delete_failure_rolls_back_stock(fake_db, monkeypatch):
    from fastapi.testclient import TestClient

    from app.main import app

    _seed_reservation(fake_db)
    _fail_on(fake_db, monkeypatch, "DELETE FROM reservations")

    client = TestClient(app, raise_server_exceptions=False)
    resp = client.post(
        "/reservations/order-1/release", headers={"X-Tenant-Id": "tenant-a"}
    )

    assert resp.status_code == 500
    assert len(fake_db.reservations) == 1
    assert fake_db.items[0]["quantity"] == 5


def test_release_reservation_twice_restores_stock_once(client, fake_db):
    fake_db.add_item(sku="WIDGET", warehouse_id="w1", quantity=10, tenant_id="tenant-a")
    reserve = client.post(
        "/items/reserve",
        json={"sku": "WIDGET", "warehouse_id": "w1", "quantity": 4, "order_id": "o-2"},
        headers={"X-Tenant-Id": "tenant-a"},
    )
    assert reserve.status_code == 200
    assert fake_db.items[0]["quantity"] == 6

    first = client.post(
        "/reservations/o-2/release", headers={"X-Tenant-Id": "tenant-a"}
    )
    assert first.status_code == 200
    assert first.json() == {"order_id": "o-2", "released": 4}
    assert fake_db.items[0]["quantity"] == 10

    second = client.post(
        "/reservations/o-2/release", headers={"X-Tenant-Id": "tenant-a"}
    )
    assert second.status_code == 404
    # Stock went back up by the reserved quantity exactly once.
    assert fake_db.items[0]["quantity"] == 10
    assert fake_db.reservations == []


def test_release_nonexistent_reservation_leaves_stock_unchanged(client, fake_db):
    fake_db.add_item(sku="WIDGET", warehouse_id="w1", quantity=5, tenant_id="tenant-a")
    resp = client.post(
        "/reservations/never-existed/release", headers={"X-Tenant-Id": "tenant-a"}
    )
    assert resp.status_code == 404
    assert fake_db.items[0]["quantity"] == 5


def test_release_other_tenants_reservation_is_404(client, fake_db):
    _seed_reservation(fake_db)
    resp = client.post(
        "/reservations/order-1/release", headers={"X-Tenant-Id": "tenant-b"}
    )
    assert resp.status_code == 404
    assert len(fake_db.reservations) == 1
    assert fake_db.items[0]["quantity"] == 5
