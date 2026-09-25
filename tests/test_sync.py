import copy
import os

import pytest
from fastapi.testclient import TestClient

from app import cache as cache_module
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
        self._rows = []

    def execute(self, sql, params=()):
        if self._fail_on and sql.startswith(self._fail_on):
            raise RuntimeError(f"injected failure on {self._fail_on!r}")
        if "RETURNING" in sql:
            self.rowcount, self._rows = self._fake_db.execute_returning(sql, params)
        else:
            self._rows = []
            self.rowcount = self._fake_db.execute(sql, params)

    def fetchone(self):
        return self._rows[0] if self._rows else None


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


# -- idempotent release: stock is restored at most once per reservation --
#
# Chosen contract: a second release (or a release of an id that never
# existed / belongs to another tenant) claims no reservation row, leaves
# stock untouched and returns 404 -- the endpoint's existing "missing
# reservation" response.


def test_release_reservation_twice_restores_stock_once(client, fake_db):
    _seed_release(fake_db)
    headers = {"X-Tenant-Id": "tenant-a"}
    first = client.post("/reservations/order-1/release", headers=headers)
    assert first.status_code == 200
    assert first.json() == {"order_id": "order-1", "released": 3}
    second = client.post("/reservations/order-1/release", headers=headers)
    assert second.status_code == 404
    assert fake_db.items[0]["quantity"] == 8
    assert fake_db.reservations == []


def test_release_reservation_twice_restores_stock_once_real_txn(fake_db, real_txn):
    _, conns = real_txn
    _seed_release(fake_db)
    client = TestClient(app)
    headers = {"X-Tenant-Id": "tenant-a"}
    assert (
        client.post("/reservations/order-1/release", headers=headers).status_code == 200
    )
    assert (
        client.post("/reservations/order-1/release", headers=headers).status_code == 404
    )
    assert fake_db.items[0]["quantity"] == 8
    # Second call claimed nothing, so its transaction committed no writes.
    assert len(conns) == 2
    assert conns[0].committed
    assert not conns[1].committed


def test_release_nonexistent_reservation_does_not_touch_stock(client, fake_db):
    _seed_release(fake_db)
    resp = client.post(
        "/reservations/does-not-exist/release", headers={"X-Tenant-Id": "tenant-a"}
    )
    assert resp.status_code == 404
    assert fake_db.items[0]["quantity"] == 5
    assert len(fake_db.reservations) == 1


def test_release_other_tenants_reservation_claims_nothing(client, fake_db):
    _seed_release(fake_db)
    fake_db.add_item(sku="WIDGET", warehouse_id="w1", quantity=5, tenant_id="tenant-b")
    resp = client.post(
        "/reservations/order-1/release", headers={"X-Tenant-Id": "tenant-b"}
    )
    assert resp.status_code == 404
    assert [i["quantity"] for i in fake_db.items] == [5, 5]
    assert len(fake_db.reservations) == 1
    assert fake_db.reservations[0]["tenant_id"] == "tenant-a"


# -- cache correctness: release invalidates the exact key GET /stock reads --


def _get_stock(client, tenant, warehouse, sku="WIDGET"):
    resp = client.get(
        f"/items/{sku}/stock",
        params={"warehouse_id": warehouse},
        headers={"X-Tenant-Id": tenant},
    )
    assert resp.status_code == 200
    return resp.json()["quantity"]


def test_release_then_get_stock_returns_restored_quantity(client, fake_db):
    _seed_release(fake_db)
    # Warm the cache with the pre-release quantity.
    assert _get_stock(client, "tenant-a", "w1") == 5
    resp = client.post(
        "/reservations/order-1/release", headers={"X-Tenant-Id": "tenant-a"}
    )
    assert resp.status_code == 200
    assert _get_stock(client, "tenant-a", "w1") == 8


def test_reserve_release_roundtrip_get_stock_is_fresh(client, fake_db):
    fake_db.add_item(sku="WIDGET", warehouse_id="w1", quantity=10, tenant_id="tenant-a")
    headers = {"X-Tenant-Id": "tenant-a"}
    assert _get_stock(client, "tenant-a", "w1") == 10
    body = {"sku": "WIDGET", "warehouse_id": "w1", "quantity": 4, "order_id": "o-9"}
    assert client.post("/items/reserve", json=body, headers=headers).status_code == 200
    assert _get_stock(client, "tenant-a", "w1") == 6
    assert client.post("/reservations/o-9/release", headers=headers).status_code == 200
    assert _get_stock(client, "tenant-a", "w1") == 10


def test_release_does_not_disturb_other_tenant_or_warehouse_cache(client, fake_db):
    _seed_release(fake_db)  # tenant-a / w1 / WIDGET: 5, reservation of 3
    fake_db.add_item(sku="WIDGET", warehouse_id="w2", quantity=11, tenant_id="tenant-a")
    fake_db.add_item(sku="WIDGET", warehouse_id="w1", quantity=20, tenant_id="tenant-b")
    # Warm all three entries; each must be scoped to its own tenant+warehouse.
    assert _get_stock(client, "tenant-a", "w1") == 5
    assert _get_stock(client, "tenant-a", "w2") == 11
    assert _get_stock(client, "tenant-b", "w1") == 20
    other_keys = {
        cache_module.stock_cache_key("tenant-a", "w2", "WIDGET"),
        cache_module.stock_cache_key("tenant-b", "w1", "WIDGET"),
    }
    assert other_keys <= set(cache_module._store)

    resp = client.post(
        "/reservations/order-1/release", headers={"X-Tenant-Id": "tenant-a"}
    )
    assert resp.status_code == 200

    # Unrelated entries are left in place and still correct.
    assert other_keys <= set(cache_module._store)
    assert _get_stock(client, "tenant-a", "w1") == 8
    assert _get_stock(client, "tenant-a", "w2") == 11
    assert _get_stock(client, "tenant-b", "w1") == 20


def test_get_and_release_share_stock_cache_key(client, fake_db):
    _seed_release(fake_db)
    _get_stock(client, "tenant-a", "w1")
    key = cache_module.stock_cache_key("tenant-a", "w1", "WIDGET")
    # The GET populated exactly the helper-built key ...
    assert cache_module.get(key) == 5
    assert (
        client.post(
            "/reservations/order-1/release", headers={"X-Tenant-Id": "tenant-a"}
        ).status_code
        == 200
    )
    # ... and the release removed exactly that key.
    assert key not in cache_module._store


def test_stock_cache_key_scopes_every_component():
    k = cache_module.stock_cache_key
    base = k("t", "w", "s")
    assert base == k("t", "w", "s")
    assert len({base, k("t2", "w", "s"), k("t", "w2", "s"), k("t", "w", "s2")}) == 4
    # A separator inside a component must not collide with another split.
    assert k("a:b", "c", "s") != k("a", "b:c", "s")


def test_release_invalidates_cache_only_after_commit(fake_db, real_txn, monkeypatch):
    _, conns = real_txn
    _seed_release(fake_db)
    committed_at_invalidate = []
    real_invalidate = cache_module.invalidate_stock

    def spy(*args):
        committed_at_invalidate.append(conns[-1].committed)
        real_invalidate(*args)

    monkeypatch.setattr(cache_module, "invalidate_stock", spy)
    client = TestClient(app)
    resp = client.post(
        "/reservations/order-1/release", headers={"X-Tenant-Id": "tenant-a"}
    )
    assert resp.status_code == 200
    assert committed_at_invalidate == [True]


def test_failed_release_leaves_cache_warm_and_correct(fake_db, real_txn):
    fail_on, conns = real_txn
    _seed_release(fake_db)
    client = TestClient(app, raise_server_exceptions=False)
    assert _get_stock(client, "tenant-a", "w1") == 5
    fail_on("UPDATE items SET quantity = quantity + %s")
    resp = client.post(
        "/reservations/order-1/release", headers={"X-Tenant-Id": "tenant-a"}
    )
    assert resp.status_code >= 500
    assert conns[-1].rolled_back
    # Rolled back: stock unchanged, and the cached value is still right.
    assert _get_stock(client, "tenant-a", "w1") == 5
