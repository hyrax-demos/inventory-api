import copy
import os

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from app import cache
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
        self._rows = []

    def execute(self, sql, params=()):
        if self._conn._fail_on and sql.startswith(self._conn._fail_on):
            raise RuntimeError(f"injected failure: {self._conn._fail_on}")
        if "RETURNING" in sql:
            self._rows = self._conn._fake_db.execute_returning(sql, params)
            self.rowcount = len(self._rows)
        else:
            self.rowcount = self._conn._fake_db.execute(sql, params)

    def fetchone(self):
        return self._rows[0] if self._rows else None


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


# -- idempotent release ------------------------------------------------------
#
# The route claims the reservation (DELETE ... RETURNING) before touching
# stock and restores only from the row it actually claimed. A repeat release
# claims nothing, returns 404, and leaves stock alone.


def _release(client, order_id="order-1", tenant="tenant-a"):
    return client.post(
        f"/reservations/{order_id}/release", headers={"X-Tenant-Id": tenant}
    )


def test_release_reservation_twice_restores_stock_once(fake_db, real_tx):
    _seed(fake_db)
    client = TestClient(app)
    first = _release(client)
    assert first.status_code == 200
    assert first.json() == {"order_id": "order-1", "released": 3}
    second = _release(client)
    assert second.status_code == 404
    assert fake_db.items[0]["quantity"] == 8
    assert fake_db.reservations == []
    # The no-op second call rolled back its (empty) transaction.
    assert real_tx["connections"][1].committed is False


def test_release_reservation_twice_with_default_fake(client, fake_db):
    _seed(fake_db)
    assert _release(client).status_code == 200
    assert _release(client).status_code == 404
    assert fake_db.items[0]["quantity"] == 8


def test_release_nonexistent_reservation_leaves_stock(fake_db, real_tx):
    _seed(fake_db)
    client = TestClient(app)
    resp = _release(client, order_id="does-not-exist")
    assert resp.status_code == 404
    assert fake_db.items[0]["quantity"] == 5
    assert len(fake_db.reservations) == 1


def test_release_other_tenants_reservation_is_404(fake_db, real_tx):
    _seed(fake_db)
    fake_db.add_item(sku="WIDGET", warehouse_id="w1", quantity=5, tenant_id="tenant-b")
    client = TestClient(app)
    resp = _release(client, tenant="tenant-b")
    assert resp.status_code == 404
    assert [i["quantity"] for i in fake_db.items] == [5, 5]
    assert len(fake_db.reservations) == 1


def test_release_restores_claimed_quantity_not_stale_value(fake_db, real_tx):
    # The restore uses the quantity from the row the DELETE claimed.
    _seed(fake_db)
    fake_db.reservations[0]["quantity"] = 4
    client = TestClient(app)
    resp = _release(client)
    assert resp.json()["released"] == 4
    assert fake_db.items[0]["quantity"] == 9


def test_release_retry_after_failed_restore_restores_once(fake_db, real_tx):
    _seed(fake_db)
    real_tx["fail_on"] = "UPDATE items SET quantity = quantity + %s"
    failing = TestClient(app, raise_server_exceptions=False)
    assert _release(failing).status_code == 500
    assert len(fake_db.reservations) == 1
    real_tx["fail_on"] = None
    client = TestClient(app)
    assert _release(client).status_code == 200
    assert _release(client).status_code == 404
    assert fake_db.items[0]["quantity"] == 8


def test_racing_releases_restore_stock_once(fake_db, real_tx, monkeypatch):
    """Two releases interleave: the second runs to completion between the
    first's claim and its stock restore. Only one may restore stock."""
    _seed(fake_db)
    original = fake_db.execute_returning
    outcomes = []

    def interleaved(sql, params=()):
        rows = original(sql, params)
        if not outcomes:
            outcomes.append("racing")
            try:
                sync_routes.release_reservation("order-1", x_tenant_id="tenant-a")
                outcomes.append("second-succeeded")
            except HTTPException as exc:
                outcomes.append(exc.status_code)
        return rows

    monkeypatch.setattr(fake_db, "execute_returning", interleaved)
    client = TestClient(app)
    resp = _release(client)
    assert resp.status_code == 200
    assert resp.json()["released"] == 3
    assert outcomes == ["racing", 404]
    assert fake_db.items[0]["quantity"] == 8
    assert fake_db.reservations == []


# -- stock cache invalidation on release -------------------------------------
#
# GET /items/{sku}/stock reads through ``cache.stock_key``; release must drop
# exactly that key, and only once its transaction has committed.


def _get_stock(client, sku="WIDGET", warehouse_id="w1", tenant="tenant-a"):
    return client.get(
        f"/items/{sku}/stock",
        params={"warehouse_id": warehouse_id},
        headers={"X-Tenant-Id": tenant},
    )


def test_release_then_get_stock_returns_restored_quantity(fake_db, real_tx):
    _seed(fake_db)
    client = TestClient(app)
    warm = _get_stock(client)
    assert warm.json()["quantity"] == 5
    assert cache.get(cache.stock_key("WIDGET")) == 5

    assert _release(client).status_code == 200

    fresh = _get_stock(client)
    assert fresh.status_code == 200
    assert fresh.json() == {"sku": "WIDGET", "warehouse_id": "w1", "quantity": 8}


def test_release_then_get_stock_with_default_fake(client, fake_db):
    _seed(fake_db)
    assert _get_stock(client).json()["quantity"] == 5
    assert _release(client).status_code == 200
    assert _get_stock(client).json()["quantity"] == 8


def test_failed_release_does_not_invalidate_stock_cache(fake_db, real_tx):
    _seed(fake_db)
    client = TestClient(app, raise_server_exceptions=False)
    assert _get_stock(client).json()["quantity"] == 5
    real_tx["fail_on"] = "UPDATE items SET quantity = quantity + %s"
    assert _release(client).status_code == 500
    # Rolled back: stock unchanged and the cached value is still valid.
    assert cache.get(cache.stock_key("WIDGET")) == 5
    assert _get_stock(client).json()["quantity"] == 5


def test_noop_release_does_not_invalidate_stock_cache(fake_db, real_tx):
    _seed(fake_db)
    client = TestClient(app)
    assert _get_stock(client).json()["quantity"] == 5
    assert _release(client, order_id="does-not-exist").status_code == 404
    assert cache.get(cache.stock_key("WIDGET")) == 5


def test_release_leaves_other_rows_and_cache_entries_intact(fake_db, real_tx):
    _seed(fake_db)
    fake_db.add_item(sku="WIDGET", warehouse_id="w2", quantity=11, tenant_id="tenant-a")
    fake_db.add_item(sku="WIDGET", warehouse_id="w1", quantity=7, tenant_id="tenant-b")
    fake_db.add_item(sku="GADGET", warehouse_id="w1", quantity=2, tenant_id="tenant-a")
    client = TestClient(app)
    assert _get_stock(client, sku="GADGET").json()["quantity"] == 2

    assert _release(client).status_code == 200

    # Only the reserved tenant/warehouse/sku row was restored.
    quantities = {
        (i["tenant_id"], i["warehouse_id"], i["sku"]): i["quantity"]
        for i in fake_db.items
    }
    assert quantities == {
        ("tenant-a", "w1", "WIDGET"): 8,
        ("tenant-a", "w2", "WIDGET"): 11,
        ("tenant-b", "w1", "WIDGET"): 7,
        ("tenant-a", "w1", "GADGET"): 2,
    }
    # An unrelated SKU's cached stock is not cleared.
    assert cache.get(cache.stock_key("GADGET")) == 2
