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


# -- atomic release ----------------------------------------------------------
#
# These drive the real ``app.db.transaction()`` against a fake psycopg2-style
# connection (``get_connection`` is patched), so the commit/rollback path the
# route relies on is the production one. The fake connection snapshots the
# FakeDB store when opened and restores it on ``rollback()``, and its cursor
# can be told to raise on a given statement.


class _TxConnection:
    def __init__(self, fake_db, fail_on):
        self._fake_db = fake_db
        self._fail_on = fail_on
        self._snapshot = (
            copy.deepcopy(fake_db.items),
            copy.deepcopy(fake_db.reservations),
        )
        self.committed = False
        self.rolled_back = False

    def cursor(self, cursor_factory=None):
        return _TxCursor(self)

    def commit(self):
        self.committed = True

    def rollback(self):
        self.rolled_back = True
        self._fake_db.items, self._fake_db.reservations = self._snapshot

    def close(self):
        pass


class _TxCursor:
    def __init__(self, conn):
        self._conn = conn
        self.rowcount = 0

    def execute(self, sql, params=()):
        if self._conn._fail_on and sql.startswith(self._conn._fail_on):
            raise RuntimeError(f"injected failure: {self._conn._fail_on}")
        self.rowcount = self._conn._fake_db.execute(sql, params)


@pytest.fixture
def real_tx(fake_db, monkeypatch):
    """Route ``release_reservation`` through the real ``db.transaction``."""
    state = {"fail_on": None, "connections": []}

    def get_connection():
        conn = _TxConnection(fake_db, state["fail_on"])
        state["connections"].append(conn)
        return conn

    monkeypatch.setattr(db_module, "get_connection", get_connection)
    monkeypatch.setattr(sync_routes, "transaction", db_module.transaction)
    return state


def _seed(fake_db):
    fake_db.add_item(sku="WIDGET", warehouse_id="w1", quantity=5, tenant_id="tenant-a")
    fake_db.add_reservation(
        order_id="order-1",
        tenant_id="tenant-a",
        sku="WIDGET",
        warehouse_id="w1",
        quantity=3,
    )


def test_release_reservation_atomic_happy_path(fake_db, real_tx):
    _seed(fake_db)
    client = TestClient(app)
    resp = client.post(
        "/reservations/order-1/release", headers={"X-Tenant-Id": "tenant-a"}
    )
    assert resp.status_code == 200
    assert resp.json() == {"order_id": "order-1", "released": 3}
    assert fake_db.items[0]["quantity"] == 8
    assert fake_db.reservations == []
    # Both statements ran on a single connection with a single commit.
    assert len(real_tx["connections"]) == 1
    assert real_tx["connections"][0].committed is True


def test_release_reservation_restore_failure_keeps_reservation(fake_db, real_tx):
    _seed(fake_db)
    real_tx["fail_on"] = "UPDATE items SET quantity = quantity + %s"
    client = TestClient(app, raise_server_exceptions=False)
    resp = client.post(
        "/reservations/order-1/release", headers={"X-Tenant-Id": "tenant-a"}
    )
    assert resp.status_code == 500
    assert fake_db.items[0]["quantity"] == 5
    assert len(fake_db.reservations) == 1
    assert fake_db.reservations[0]["order_id"] == "order-1"
    conn = real_tx["connections"][0]
    assert conn.rolled_back is True
    assert conn.committed is False


def test_release_reservation_delete_failure_does_not_restore_stock(fake_db, real_tx):
    _seed(fake_db)
    real_tx["fail_on"] = "DELETE FROM reservations"
    client = TestClient(app, raise_server_exceptions=False)
    resp = client.post(
        "/reservations/order-1/release", headers={"X-Tenant-Id": "tenant-a"}
    )
    assert resp.status_code == 500
    assert fake_db.items[0]["quantity"] == 5
    assert len(fake_db.reservations) == 1
    conn = real_tx["connections"][0]
    assert conn.rolled_back is True
    assert conn.committed is False
