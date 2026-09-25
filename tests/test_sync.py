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


def test_release_reservation_twice_restores_stock_once(client, fake_db):
    fake_db.add_item(sku="GADGET", warehouse_id="w1", quantity=10, tenant_id="tenant-a")
    fake_db.add_reservation(
        order_id="order-4",
        tenant_id="tenant-a",
        sku="GADGET",
        warehouse_id="w1",
        quantity=4,
    )
    headers = {"X-Tenant-Id": "tenant-a"}

    first = client.post("/reservations/order-4/release", headers=headers)
    assert first.status_code == 200
    stock_after_first = fake_db.items[0]["quantity"]
    assert stock_after_first == 14

    second = client.post("/reservations/order-4/release", headers=headers)
    assert second.status_code == 404
    assert fake_db.items[0]["quantity"] == stock_after_first


def test_release_unknown_reservation_leaves_stock_unchanged(client, fake_db):
    fake_db.add_item(sku="GADGET", warehouse_id="w1", quantity=10, tenant_id="tenant-a")
    resp = client.post(
        "/reservations/no-such-order/release", headers={"X-Tenant-Id": "tenant-a"}
    )
    assert resp.status_code == 404
    assert fake_db.items[0]["quantity"] == 10


def test_release_other_tenants_reservation_is_not_found(client, fake_db):
    fake_db.add_item(sku="GADGET", warehouse_id="w1", quantity=10, tenant_id="tenant-a")
    fake_db.add_item(sku="GADGET", warehouse_id="w1", quantity=7, tenant_id="tenant-b")
    fake_db.add_reservation(
        order_id="order-5",
        tenant_id="tenant-b",
        sku="GADGET",
        warehouse_id="w1",
        quantity=2,
    )
    resp = client.post(
        "/reservations/order-5/release", headers={"X-Tenant-Id": "tenant-a"}
    )
    assert resp.status_code == 404
    assert [i["quantity"] for i in fake_db.items] == [10, 7]
    assert len(fake_db.reservations) == 1


def test_release_reservation_lookup_locks_the_row(fake_db, monkeypatch):
    # The existence check must be a locking read so a concurrent release of
    # the same reservation cannot also pass it before this one commits.
    from fastapi.testclient import TestClient

    from app.main import app

    fake_db.add_item(sku="GADGET", warehouse_id="w1", quantity=10, tenant_id="tenant-a")
    fake_db.add_reservation(
        order_id="order-6",
        tenant_id="tenant-a",
        sku="GADGET",
        warehouse_id="w1",
        quantity=1,
    )
    seen = []
    real_fetch_all = fake_db.fetch_all

    def recording_fetch_all(sql, params=()):
        seen.append(sql)
        return real_fetch_all(sql, params)

    monkeypatch.setattr(fake_db, "fetch_all", recording_fetch_all)
    resp = TestClient(app).post(
        "/reservations/order-6/release", headers={"X-Tenant-Id": "tenant-a"}
    )
    assert resp.status_code == 200
    lookups = [s for s in seen if "FROM reservations" in s]
    assert lookups and all("FOR UPDATE" in s for s in lookups)
    assert all("tenant_id = %s" in s for s in lookups)


def test_concurrent_release_restores_stock_once():
    import pytest

    pytest.skip(
        "test backend is an in-process FakeDB with no real row locks; "
        "concurrent release needs a real Postgres with separate connections"
    )
