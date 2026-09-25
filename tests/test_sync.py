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


def test_release_reservation_restores_stock_and_removes_reservation(client, fake_db):
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
    assert resp.json() == {"order_id": "order-1", "released": 3}
    assert fake_db.items[0]["quantity"] == 8
    assert fake_db.reservations == []


def test_release_reservation_restore_failure_keeps_reservation(fake_db, monkeypatch):
    from fastapi.testclient import TestClient

    from app.main import app

    fake_db.add_item(sku="WIDGET", warehouse_id="w1", quantity=5, tenant_id="tenant-a")
    fake_db.add_reservation(
        order_id="order-1",
        tenant_id="tenant-a",
        sku="WIDGET",
        warehouse_id="w1",
        quantity=3,
    )

    real_execute = fake_db.execute

    def failing_execute(sql, params=()):
        if sql.startswith("UPDATE items SET quantity = quantity + %s"):
            raise RuntimeError("simulated stock-restore failure")
        return real_execute(sql, params)

    monkeypatch.setattr(fake_db, "execute", failing_execute)

    client = TestClient(app, raise_server_exceptions=False)
    resp = client.post(
        "/reservations/order-1/release", headers={"X-Tenant-Id": "tenant-a"}
    )

    assert resp.status_code == 500
    # The reservation must survive so the release can be retried...
    assert len(fake_db.reservations) == 1
    assert fake_db.reservations[0]["order_id"] == "order-1"
    # ...and the stock level is untouched.
    assert fake_db.items[0]["quantity"] == 5


# Idempotency convention: releasing a reservation that is already gone
# (released earlier, or never existed) returns 404 and never touches stock.


def test_release_reservation_twice_restores_stock_once(client, fake_db):
    fake_db.add_item(sku="WIDGET", warehouse_id="w1", quantity=5, tenant_id="tenant-a")
    fake_db.add_reservation(
        order_id="order-1",
        tenant_id="tenant-a",
        sku="WIDGET",
        warehouse_id="w1",
        quantity=3,
    )
    first = client.post(
        "/reservations/order-1/release", headers={"X-Tenant-Id": "tenant-a"}
    )
    assert first.status_code == 200
    assert fake_db.items[0]["quantity"] == 8

    second = client.post(
        "/reservations/order-1/release", headers={"X-Tenant-Id": "tenant-a"}
    )
    assert second.status_code == 404
    # Stock was restored exactly once.
    assert fake_db.items[0]["quantity"] == 8
    assert fake_db.reservations == []


def test_release_unknown_reservation_changes_no_stock(client, fake_db):
    fake_db.add_item(sku="WIDGET", warehouse_id="w1", quantity=5, tenant_id="tenant-a")
    fake_db.add_reservation(
        order_id="order-1",
        tenant_id="tenant-a",
        sku="WIDGET",
        warehouse_id="w1",
        quantity=3,
    )
    resp = client.post(
        "/reservations/does-not-exist/release", headers={"X-Tenant-Id": "tenant-a"}
    )
    assert resp.status_code == 404
    assert fake_db.items[0]["quantity"] == 5
    assert len(fake_db.reservations) == 1


def test_release_reservation_other_tenant_is_404_and_changes_no_stock(client, fake_db):
    fake_db.add_item(sku="WIDGET", warehouse_id="w1", quantity=5, tenant_id="tenant-a")
    fake_db.add_reservation(
        order_id="order-1",
        tenant_id="tenant-a",
        sku="WIDGET",
        warehouse_id="w1",
        quantity=3,
    )
    resp = client.post(
        "/reservations/order-1/release", headers={"X-Tenant-Id": "tenant-b"}
    )
    assert resp.status_code == 404
    assert fake_db.items[0]["quantity"] == 5
    assert len(fake_db.reservations) == 1


# Cache correctness: a release must invalidate exactly the cache entry that
# GET /items/{sku}/stock reads, and only once the release has committed. These
# tests go through the real in-process cache (app.cache), not a mock.


def test_release_reservation_refreshes_cached_stock(client, fake_db):
    from app import cache

    fake_db.add_item(sku="WIDGET", warehouse_id="w1", quantity=5, tenant_id="tenant-a")
    fake_db.add_reservation(
        order_id="order-1",
        tenant_id="tenant-a",
        sku="WIDGET",
        warehouse_id="w1",
        quantity=3,
    )
    headers = {"X-Tenant-Id": "tenant-a"}

    # Warm the cache.
    warm = client.get(
        "/items/WIDGET/stock", params={"warehouse_id": "w1"}, headers=headers
    )
    assert warm.status_code == 200
    assert warm.json()["quantity"] == 5
    assert cache.get(cache.stock_key("WIDGET")) == 5

    resp = client.post("/reservations/order-1/release", headers=headers)
    assert resp.status_code == 200

    # The entry the GET handler reads has been dropped...
    assert cache.get(cache.stock_key("WIDGET")) is None
    # ...so the next read returns the freshly restored quantity, not stale 5.
    fresh = client.get(
        "/items/WIDGET/stock", params={"warehouse_id": "w1"}, headers=headers
    )
    assert fresh.status_code == 200
    assert fresh.json()["quantity"] == 8


def test_release_reservation_leaves_other_skus_cached_stock(client, fake_db):
    from app import cache

    fake_db.add_item(sku="WIDGET", warehouse_id="w1", quantity=5, tenant_id="tenant-a")
    fake_db.add_item(sku="GADGET", warehouse_id="w1", quantity=7, tenant_id="tenant-a")
    fake_db.add_reservation(
        order_id="order-1",
        tenant_id="tenant-a",
        sku="WIDGET",
        warehouse_id="w1",
        quantity=3,
    )
    headers = {"X-Tenant-Id": "tenant-a"}
    for sku in ("WIDGET", "GADGET"):
        r = client.get(
            f"/items/{sku}/stock", params={"warehouse_id": "w1"}, headers=headers
        )
        assert r.status_code == 200

    resp = client.post("/reservations/order-1/release", headers=headers)
    assert resp.status_code == 200

    # Only the released SKU's entry is invalidated.
    assert cache.get(cache.stock_key("WIDGET")) is None
    assert cache.get(cache.stock_key("GADGET")) == 7


def test_release_reservation_failed_restore_keeps_cached_stock(fake_db, monkeypatch):
    # No invalidation happens unless the release transaction committed.
    from fastapi.testclient import TestClient

    from app import cache
    from app.main import app

    fake_db.add_item(sku="WIDGET", warehouse_id="w1", quantity=5, tenant_id="tenant-a")
    fake_db.add_reservation(
        order_id="order-1",
        tenant_id="tenant-a",
        sku="WIDGET",
        warehouse_id="w1",
        quantity=3,
    )
    client = TestClient(app, raise_server_exceptions=False)
    headers = {"X-Tenant-Id": "tenant-a"}
    client.get("/items/WIDGET/stock", params={"warehouse_id": "w1"}, headers=headers)
    assert cache.get(cache.stock_key("WIDGET")) == 5

    real_execute = fake_db.execute

    def failing_execute(sql, params=()):
        if sql.startswith("UPDATE items SET quantity = quantity + %s"):
            raise RuntimeError("simulated stock-restore failure")
        return real_execute(sql, params)

    monkeypatch.setattr(fake_db, "execute", failing_execute)

    resp = client.post("/reservations/order-1/release", headers=headers)
    assert resp.status_code == 500
    assert cache.get(cache.stock_key("WIDGET")) == 5
