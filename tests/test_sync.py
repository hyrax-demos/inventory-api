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


# -- atomic release: stock restore + reservation delete in one transaction --


class _FailingCursor:
    """Cursor over a FakeDB that raises on the first statement whose SQL
    starts with ``fail_on``."""

    def __init__(self, fake_db, fail_on):
        self._fake_db = fake_db
        self._fail_on = fail_on
        self.rowcount = 0

    def execute(self, sql, params=()):
        if self._fail_on and sql.startswith(self._fail_on):
            raise RuntimeError(f"injected failure on {self._fail_on!r}")
        self.rowcount = self._fake_db.execute(sql, params)


class _RollbackConnection:
    """Fake psycopg2 connection with real rollback semantics over a FakeDB.

    Snapshots the FakeDB's tables when opened; ``rollback`` restores them,
    ``commit`` keeps whatever the cursor wrote.
    """

    def __init__(self, fake_db, fail_on=None):
        self._fake_db = fake_db
        self._fail_on = fail_on
        self._snapshot = (
            copy.deepcopy(fake_db.items),
            copy.deepcopy(fake_db.reservations),
        )
        self.committed = False
        self.rolled_back = False

    def cursor(self, cursor_factory=None):
        return _FailingCursor(self._fake_db, self._fail_on)

    def commit(self):
        self.committed = True

    def rollback(self):
        self.rolled_back = True
        self._fake_db.items, self._fake_db.reservations = (
            copy.deepcopy(self._snapshot[0]),
            copy.deepcopy(self._snapshot[1]),
        )

    def close(self):
        pass


@pytest.fixture
def real_txn(monkeypatch, fake_db):
    """Route release_reservation through the real ``app.db.transaction``
    backed by a rollback-capable fake connection. Returns a setter for the
    SQL prefix to fail on, and the list of connections opened."""
    state = {"fail_on": None}
    conns = []

    def _connect():
        conn = _RollbackConnection(fake_db, state["fail_on"])
        conns.append(conn)
        return conn

    monkeypatch.setattr(db_module, "get_connection", _connect)
    monkeypatch.setattr(sync_routes, "transaction", db_module.transaction)

    def fail_on(prefix):
        state["fail_on"] = prefix

    return fail_on, conns


def _seed_release(fake_db):
    fake_db.add_item(sku="WIDGET", warehouse_id="w1", quantity=5, tenant_id="tenant-a")
    fake_db.add_reservation(
        order_id="order-1",
        tenant_id="tenant-a",
        sku="WIDGET",
        warehouse_id="w1",
        quantity=3,
    )


def test_release_reservation_atomic_happy_path(client, fake_db, real_txn):
    _, conns = real_txn
    _seed_release(fake_db)
    resp = client.post(
        "/reservations/order-1/release", headers={"X-Tenant-Id": "tenant-a"}
    )
    assert resp.status_code == 200
    assert resp.json() == {"order_id": "order-1", "released": 3}
    assert fake_db.items[0]["quantity"] == 8
    assert fake_db.reservations == []
    # Both statements ran on a single connection that committed once.
    assert len(conns) == 1
    assert conns[0].committed and not conns[0].rolled_back


def test_release_reservation_restore_failure_keeps_reservation(fake_db, real_txn):
    fail_on, conns = real_txn
    _seed_release(fake_db)
    fail_on("UPDATE items SET quantity = quantity + %s")
    client = TestClient(app, raise_server_exceptions=False)
    resp = client.post(
        "/reservations/order-1/release", headers={"X-Tenant-Id": "tenant-a"}
    )
    assert resp.status_code >= 500
    assert fake_db.items[0]["quantity"] == 5
    assert len(fake_db.reservations) == 1
    assert fake_db.reservations[0]["order_id"] == "order-1"
    assert conns[0].rolled_back and not conns[0].committed


def test_release_reservation_delete_failure_rolls_back_restore(fake_db, real_txn):
    fail_on, conns = real_txn
    _seed_release(fake_db)
    fail_on("DELETE FROM reservations")
    client = TestClient(app, raise_server_exceptions=False)
    resp = client.post(
        "/reservations/order-1/release", headers={"X-Tenant-Id": "tenant-a"}
    )
    assert resp.status_code >= 500
    # The restore ran first but must have been rolled back with the delete.
    assert fake_db.items[0]["quantity"] == 5
    assert len(fake_db.reservations) == 1
    assert conns[0].rolled_back and not conns[0].committed
