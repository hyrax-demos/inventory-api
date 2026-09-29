import copy
import os

import pytest
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


# -- atomic release -----------------------------------------------------------
# The shared FakeDB.transaction() cannot roll back, so these tests drive the
# real ``app.db.transaction()`` over a fake connection whose ``rollback()``
# restores a snapshot of the in-memory store taken when it was opened.


class _RollbackCursor:
    """Routes writes to the FakeDB so tests can monkeypatch its ``execute``."""

    def __init__(self, fake_db):
        self._fake_db = fake_db
        self.rowcount = 0
        self._rows = []

    def execute(self, sql, params=()):
        result = self._fake_db.execute(sql, params)
        if isinstance(result, list):  # a ``... RETURNING`` statement
            self._rows = result
            self.rowcount = len(result)
        else:
            self.rowcount = result

    def fetchone(self):
        return self._rows[0] if self._rows else None


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


# -- idempotent release ---------------------------------------------------------
# Chosen API: releasing a reservation that no longer exists (already released,
# or never created) returns 404 and never touches stock. The reservation row is
# claimed with ``DELETE ... RETURNING`` inside the transaction, so only the call
# that actually removed the row restores its quantity.


def test_release_reservation_twice_restores_stock_once(client, fake_db, real_txn):
    _seed(fake_db)
    headers = {"X-Tenant-Id": "tenant-a"}

    first = client.post("/reservations/order-1/release", headers=headers)
    assert first.status_code == 200
    assert first.json()["released"] == 3
    assert fake_db.items[0]["quantity"] == 8

    second = client.post("/reservations/order-1/release", headers=headers)
    assert second.status_code == 404
    assert fake_db.items[0]["quantity"] == 8
    assert fake_db.reservations == []
    # The second attempt changed nothing (its transaction was rolled back).
    assert real_txn[1].rolled_back and not real_txn[1].committed


def test_release_reservation_twice_restores_stock_once_shared_fake(client, fake_db):
    _seed(fake_db)
    url = "/reservations/order-1/release"
    headers = {"X-Tenant-Id": "tenant-a"}
    assert client.post(url, headers=headers).status_code == 200
    assert client.post(url, headers=headers).status_code == 404
    assert fake_db.items[0]["quantity"] == 8


def test_release_nonexistent_reservation_leaves_stock_unchanged(
    client, fake_db, real_txn
):
    _seed(fake_db)
    resp = client.post(
        "/reservations/no-such-order/release", headers={"X-Tenant-Id": "tenant-a"}
    )
    assert resp.status_code == 404
    assert fake_db.items[0]["quantity"] == 5
    assert len(fake_db.reservations) == 1


def test_release_other_tenants_reservation_leaves_stock_unchanged(
    client, fake_db, real_txn
):
    _seed(fake_db)
    resp = client.post(
        "/reservations/order-1/release", headers={"X-Tenant-Id": "tenant-b"}
    )
    assert resp.status_code == 404
    assert fake_db.items[0]["quantity"] == 5
    assert len(fake_db.reservations) == 1


# -- stock cache invalidation on release ---------------------------------------
# GET /items/{sku}/stock (app/routes/items.py::get_stock) reads and writes the
# key built by ``cache.stock_key(sku)``; release_reservation invalidates the
# key from that same builder once its transaction commits, so the next read
# reflects the restored quantity instead of the pre-release snapshot.


def _warm_then_release_then_read(client, fake_db):
    tenant = {"X-Tenant-Id": "tenant-a"}
    params = {"warehouse_id": "w1"}

    warm = client.get("/items/WIDGET/stock", params=params, headers=tenant)
    assert warm.status_code == 200
    assert warm.json()["quantity"] == 5
    # The GET populated exactly the key the shared builder produces.
    assert cache.get(cache.stock_key("WIDGET")) == 5

    # Prove the cache is live: change the row behind its back and confirm the
    # GET still serves the cached snapshot. Without this, an inert cache or a
    # never-hit key would let the post-release assertion pass trivially.
    fake_db.items[0]["quantity"] = 99
    cached = client.get("/items/WIDGET/stock", params=params, headers=tenant)
    assert cached.status_code == 200
    assert cached.json()["quantity"] == 5
    fake_db.items[0]["quantity"] = 5

    released = client.post("/reservations/order-1/release", headers=tenant)
    assert released.status_code == 200
    assert cache.get(cache.stock_key("WIDGET")) is None

    return client.get("/items/WIDGET/stock", params=params, headers=tenant)


def test_release_reservation_invalidates_cached_stock(client, fake_db):
    _seed(fake_db)
    after = _warm_then_release_then_read(client, fake_db)
    assert after.status_code == 200
    assert after.json()["quantity"] == 8


def test_release_reservation_invalidates_cached_stock_after_commit(
    client, fake_db, real_txn
):
    _seed(fake_db)
    after = _warm_then_release_then_read(client, fake_db)
    assert after.status_code == 200
    assert after.json()["quantity"] == 8
    assert real_txn[0].committed
