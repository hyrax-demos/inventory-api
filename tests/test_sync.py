import os

import pytest

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


def _rollback_transaction(fake_db, fail_on):
    """A transaction() stand-in with real rollback over FakeDB's stores.

    Snapshots items and reservations on entry, raises when a statement starting
    with ``fail_on`` runs, and restores the snapshot if the block raises, the
    same way ``app.db.transaction`` rolls back the connection on error.
    """
    import copy
    from contextlib import contextmanager

    class _Cursor:
        def execute(self, sql, params=()):
            if sql.startswith(fail_on):
                raise RuntimeError(f"simulated failure: {fail_on}")
            self.rowcount = fake_db.execute(sql, params)

    class _Conn:
        def cursor(self, cursor_factory=None):
            return _Cursor()

    @contextmanager
    def transaction():
        items = copy.deepcopy(fake_db.items)
        reservations = copy.deepcopy(fake_db.reservations)
        try:
            yield _Conn()
        except Exception:
            fake_db.items = items
            fake_db.reservations = reservations
            raise

    return transaction


def _seed_reservation(fake_db):
    fake_db.add_item(sku="WIDGET", warehouse_id="w1", quantity=5, tenant_id="tenant-a")
    fake_db.add_reservation(
        order_id="order-1",
        tenant_id="tenant-a",
        sku="WIDGET",
        warehouse_id="w1",
        quantity=3,
    )


def test_release_reservation_restores_stock_and_deletes_row(client, fake_db):
    _seed_reservation(fake_db)
    resp = client.post(
        "/reservations/order-1/release", headers={"X-Tenant-Id": "tenant-a"}
    )
    assert resp.status_code == 200
    assert fake_db.items[0]["quantity"] == 8
    assert fake_db.reservations == []


def test_release_reservation_keeps_row_when_stock_restore_fails(fake_db, monkeypatch):
    from fastapi.testclient import TestClient

    from app.main import app
    from app.routes import sync as sync_routes

    _seed_reservation(fake_db)
    monkeypatch.setattr(
        sync_routes,
        "transaction",
        _rollback_transaction(fake_db, "UPDATE items SET quantity = quantity + %s"),
    )
    client = TestClient(app, raise_server_exceptions=False)
    resp = client.post(
        "/reservations/order-1/release", headers={"X-Tenant-Id": "tenant-a"}
    )
    assert resp.status_code == 500
    assert fake_db.items[0]["quantity"] == 5
    assert len(fake_db.reservations) == 1
    assert fake_db.reservations[0]["order_id"] == "order-1"


def test_release_reservation_stock_restore_failure_propagates(fake_db, monkeypatch):
    from fastapi.testclient import TestClient

    from app.main import app
    from app.routes import sync as sync_routes

    _seed_reservation(fake_db)
    monkeypatch.setattr(
        sync_routes,
        "transaction",
        _rollback_transaction(fake_db, "UPDATE items SET quantity = quantity + %s"),
    )
    client = TestClient(app)
    with pytest.raises(RuntimeError, match="simulated failure"):
        client.post(
            "/reservations/order-1/release", headers={"X-Tenant-Id": "tenant-a"}
        )
    assert len(fake_db.reservations) == 1


def test_release_reservation_rolls_back_stock_when_delete_fails(fake_db, monkeypatch):
    from fastapi.testclient import TestClient

    from app.main import app
    from app.routes import sync as sync_routes

    _seed_reservation(fake_db)
    monkeypatch.setattr(
        sync_routes,
        "transaction",
        _rollback_transaction(fake_db, "DELETE FROM reservations"),
    )
    client = TestClient(app, raise_server_exceptions=False)
    resp = client.post(
        "/reservations/order-1/release", headers={"X-Tenant-Id": "tenant-a"}
    )
    assert resp.status_code == 500
    # The restore ran inside the same transaction and was rolled back with it.
    assert fake_db.items[0]["quantity"] == 5
    assert len(fake_db.reservations) == 1
