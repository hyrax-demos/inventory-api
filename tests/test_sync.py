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


def test_release_reservation_restores_stock_and_deletes_row(client, fake_db):
    fake_db.add_item(sku="GADGET", warehouse_id="w1", quantity=10, tenant_id="tenant-a")
    fake_db.add_reservation(
        order_id="order-2",
        tenant_id="tenant-a",
        sku="GADGET",
        warehouse_id="w1",
        quantity=4,
    )
    resp = client.post(
        "/reservations/order-2/release", headers={"X-Tenant-Id": "tenant-a"}
    )
    assert resp.status_code == 200
    assert resp.json() == {"order_id": "order-2", "released": 4}
    assert fake_db.items[0]["quantity"] == 14
    assert fake_db.reservations == []


def test_release_reservation_rolls_back_when_stock_restore_fails(fake_db, monkeypatch):
    import copy
    from contextlib import contextmanager

    from fastapi.testclient import TestClient

    from app.main import app
    from app.routes import sync as sync_routes

    fake_db.add_item(sku="GADGET", warehouse_id="w1", quantity=10, tenant_id="tenant-a")
    fake_db.add_reservation(
        order_id="order-3",
        tenant_id="tenant-a",
        sku="GADGET",
        warehouse_id="w1",
        quantity=4,
    )

    # Make the stock-restoring UPDATE blow up; everything else behaves normally.
    real_execute = fake_db.execute

    def failing_execute(sql, params=()):
        if sql.startswith("UPDATE items SET quantity = quantity + %s"):
            raise RuntimeError("simulated stock-restore failure")
        return real_execute(sql, params)

    monkeypatch.setattr(fake_db, "execute", failing_execute)

    # Give the fake transaction real rollback semantics: snapshot on entry,
    # restore on exception, so the test checks the handler's use of a single
    # transaction rather than the order of its statements.
    real_transaction = fake_db.transaction
    rollbacks = []

    @contextmanager
    def rollback_transaction():
        snapshot = (copy.deepcopy(fake_db.items), copy.deepcopy(fake_db.reservations))
        try:
            with real_transaction() as conn:
                yield conn
        except Exception:
            fake_db.items, fake_db.reservations = snapshot
            rollbacks.append(True)
            raise

    monkeypatch.setattr(sync_routes, "transaction", rollback_transaction)

    client = TestClient(app, raise_server_exceptions=False)
    resp = client.post(
        "/reservations/order-3/release", headers={"X-Tenant-Id": "tenant-a"}
    )

    assert resp.status_code >= 500
    assert rollbacks == [True]
    assert fake_db.items[0]["quantity"] == 10
    assert fake_db.reservations == [
        {
            "order_id": "order-3",
            "tenant_id": "tenant-a",
            "sku": "GADGET",
            "warehouse_id": "w1",
            "quantity": 4,
        }
    ]
