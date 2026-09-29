import copy
import os

import pytest
from fastapi.testclient import TestClient

from app import db as db_module
from app.main import app
from app.routes import sync as sync_routes

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


# -- atomic release -----------------------------------------------------------
# The shared FakeDB.transaction() cannot roll back, so these tests drive the
# real ``app.db.transaction()`` over a fake connection whose ``rollback()``
# restores a snapshot of the in-memory store taken when it was opened.


class _RollbackCursor:
    """Routes writes to the FakeDB so tests can monkeypatch its ``execute``."""

    def __init__(self, fake_db):
        self._fake_db = fake_db
        self.rowcount = 0

    def execute(self, sql, params=()):
        self.rowcount = self._fake_db.execute(sql, params)


class _RollbackConnection:
    def __init__(self, fake_db):
        self._fake_db = fake_db
        self._snapshot = (
            copy.deepcopy(fake_db.items),
            copy.deepcopy(fake_db.reservations),
        )
        self.committed = False
        self.rolled_back = False

    def cursor(self, cursor_factory=None):
        return _RollbackCursor(self._fake_db)

    def commit(self):
        self.committed = True

    def rollback(self):
        self.rolled_back = True
        self._fake_db.items, self._fake_db.reservations = copy.deepcopy(self._snapshot)

    def close(self):
        pass


@pytest.fixture
def real_txn(monkeypatch, fake_db):
    conns = []

    def _connect():
        conn = _RollbackConnection(fake_db)
        conns.append(conn)
        return conn

    monkeypatch.setattr(db_module, "get_connection", _connect)
    monkeypatch.setattr(sync_routes, "transaction", db_module.transaction)
    return conns


def _seed(fake_db):
    fake_db.add_item(sku="WIDGET", warehouse_id="w1", quantity=5, tenant_id="tenant-a")
    fake_db.add_reservation(
        order_id="order-1",
        tenant_id="tenant-a",
        sku="WIDGET",
        warehouse_id="w1",
        quantity=3,
    )


def _fail_on(monkeypatch, fake_db, prefix):
    original = fake_db.execute

    def _execute(sql, params=()):
        if sql.startswith(prefix):
            raise RuntimeError("simulated DB failure")
        return original(sql, params)

    monkeypatch.setattr(fake_db, "execute", _execute)


def test_release_reservation_commits_restore_and_delete_together(
    client, fake_db, real_txn
):
    _seed(fake_db)
    resp = client.post(
        "/reservations/order-1/release", headers={"X-Tenant-Id": "tenant-a"}
    )
    assert resp.status_code == 200
    assert fake_db.items[0]["quantity"] == 8
    assert fake_db.reservations == []
    # Both statements ran in one committed transaction.
    assert len(real_txn) == 1
    assert real_txn[0].committed and not real_txn[0].rolled_back


def test_release_reservation_stock_update_failure_keeps_reservation(
    monkeypatch, fake_db, real_txn
):
    _seed(fake_db)
    _fail_on(monkeypatch, fake_db, "UPDATE items SET quantity = quantity + %s")
    client = TestClient(app, raise_server_exceptions=False)
    resp = client.post(
        "/reservations/order-1/release", headers={"X-Tenant-Id": "tenant-a"}
    )
    assert resp.status_code == 500
    assert fake_db.items[0]["quantity"] == 5
    assert len(fake_db.reservations) == 1
    assert real_txn[0].rolled_back and not real_txn[0].committed


def test_release_reservation_delete_failure_rolls_back_stock(
    monkeypatch, fake_db, real_txn
):
    _seed(fake_db)
    _fail_on(monkeypatch, fake_db, "DELETE FROM reservations")
    client = TestClient(app, raise_server_exceptions=False)
    resp = client.post(
        "/reservations/order-1/release", headers={"X-Tenant-Id": "tenant-a"}
    )
    assert resp.status_code == 500
    assert fake_db.items[0]["quantity"] == 5
    assert len(fake_db.reservations) == 1
    assert real_txn[0].rolled_back and not real_txn[0].committed
