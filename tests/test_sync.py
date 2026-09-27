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


# -- atomic release ---------------------------------------------------------
#
# The conftest FakeDB.transaction() has no rollback semantics, so these tests
# fake one layer lower: ``app.db.get_connection`` returns a connection that
# snapshots the in-memory store when opened and restores it on rollback(). The
# route then runs through the *real* ``app.db.transaction()`` commit/rollback
# logic, which is what actually makes the restore + delete all-or-nothing.


class _TxConnection:
    """psycopg2-shaped connection over FakeDB with real rollback."""

    def __init__(self, fake_db, events, fail_on=None):
        self._fake_db = fake_db
        self._events = events
        self._fail_on = fail_on
        self._snapshot = (
            copy.deepcopy(fake_db.items),
            copy.deepcopy(fake_db.reservations),
        )

    def cursor(self, cursor_factory=None):
        return _TxCursor(self._fake_db, self._fail_on)

    def commit(self):
        self._events.append("commit")

    def rollback(self):
        self._events.append("rollback")
        self._fake_db.items[:] = self._snapshot[0]
        self._fake_db.reservations[:] = self._snapshot[1]

    def close(self):
        self._events.append("close")


class _TxCursor:
    def __init__(self, fake_db, fail_on):
        self._fake_db = fake_db
        self._fail_on = fail_on
        self.rowcount = 0
        self._rows: list = []

    def execute(self, sql, params=()):
        if self._fail_on and sql.startswith(self._fail_on):
            raise RuntimeError(f"simulated DB failure on {self._fail_on!r}")
        if "RETURNING" in sql:
            self._rows = self._fake_db.execute_returning(sql, params)
            self.rowcount = len(self._rows)
        else:
            self._rows = []
            self.rowcount = self._fake_db.execute(sql, params)

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def fetchall(self):
        return self._rows


class _TxHarness:
    def __init__(self):
        self.events: list[str] = []
        self.fail_on: str | None = None


@pytest.fixture
def tx(monkeypatch, fake_db) -> _TxHarness:
    """Route release_reservation through the real app.db.transaction()."""
    harness = _TxHarness()
    monkeypatch.setattr(
        db_module,
        "get_connection",
        lambda: _TxConnection(fake_db, harness.events, harness.fail_on),
    )
    monkeypatch.setattr(sync_routes, "transaction", db_module.transaction)
    # Record cache invalidations relative to commit/rollback.
    monkeypatch.setattr(
        sync_routes.cache,
        "invalidate",
        lambda key: harness.events.append(f"invalidate:{key}"),
    )
    return harness


def _seed_reservation(fake_db):
    fake_db.add_item(sku="WIDGET", warehouse_id="w1", quantity=5, tenant_id="tenant-a")
    fake_db.add_reservation(
        order_id="order-1",
        tenant_id="tenant-a",
        sku="WIDGET",
        warehouse_id="w1",
        quantity=3,
    )


def test_release_reservation_restores_stock_and_deletes_row_atomically(fake_db, tx):
    tx_events = tx.events
    _seed_reservation(fake_db)
    client = TestClient(app)
    resp = client.post(
        "/reservations/order-1/release", headers={"X-Tenant-Id": "tenant-a"}
    )

    assert resp.status_code == 200
    assert resp.json() == {"order_id": "order-1", "released": 3}
    assert fake_db.items[0]["quantity"] == 8
    assert fake_db.reservations == []
    # Exactly one transaction, committed, and the cache is invalidated only
    # after that commit.
    assert tx_events.count("commit") == 1
    assert "rollback" not in tx_events
    invalidations = [i for i, e in enumerate(tx_events) if e.startswith("invalidate:")]
    assert len(invalidations) == 1
    assert invalidations[0] > tx_events.index("commit")


@pytest.mark.parametrize(
    "fail_on",
    ["UPDATE items SET quantity = quantity + %s", "DELETE FROM reservations"],
    ids=["stock-restore-fails", "reservation-delete-fails"],
)
def test_release_reservation_failure_rolls_back_and_keeps_reservation(
    fake_db, tx, fail_on
):
    _seed_reservation(fake_db)
    tx.fail_on = fail_on
    tx_events = tx.events
    client = TestClient(app, raise_server_exceptions=False)
    resp = client.post(
        "/reservations/order-1/release", headers={"X-Tenant-Id": "tenant-a"}
    )

    # Not reported as a success.
    assert resp.status_code >= 500
    # Reservation row is still there and stock is unchanged.
    assert fake_db.reservations == [
        {
            "order_id": "order-1",
            "tenant_id": "tenant-a",
            "sku": "WIDGET",
            "warehouse_id": "w1",
            "quantity": 3,
        }
    ]
    assert fake_db.items[0]["quantity"] == 5
    # The transaction rolled back, never committed, and no cache invalidation
    # ran for the failed release.
    assert "rollback" in tx_events
    assert "commit" not in tx_events
    assert not any(e.startswith("invalidate:") for e in tx_events)


# -- idempotent release -----------------------------------------------------
#
# Chosen convention: a second release of the same reservation (or a release of
# one that never existed for this tenant) returns 404 "no such reservation",
# matching the route's existing not-found response, and never touches stock.
# The in-transaction DELETE ... RETURNING is the guard: stock is restored only
# by the call whose DELETE actually removed the row.


def test_release_reservation_twice_restores_stock_exactly_once(fake_db, tx):
    _seed_reservation(fake_db)
    client = TestClient(app)

    first = client.post(
        "/reservations/order-1/release", headers={"X-Tenant-Id": "tenant-a"}
    )
    assert first.status_code == 200
    assert first.json() == {"order_id": "order-1", "released": 3}
    assert fake_db.items[0]["quantity"] == 8
    events_after_first = list(tx.events)

    second = client.post(
        "/reservations/order-1/release", headers={"X-Tenant-Id": "tenant-a"}
    )
    assert second.status_code == 404
    assert second.json() == {"detail": "no such reservation"}
    # Stock went up by the reserved quantity exactly once.
    assert fake_db.items[0]["quantity"] == 8
    assert fake_db.reservations == []
    # The second call committed nothing and invalidated no cache entry.
    second_events = tx.events[len(events_after_first) :]
    assert "commit" not in second_events
    assert not any(e.startswith("invalidate:") for e in second_events)


def test_release_reservation_twice_with_default_fake(client, fake_db):
    _seed_reservation(fake_db)
    headers = {"X-Tenant-Id": "tenant-a"}

    first = client.post("/reservations/order-1/release", headers=headers)
    assert first.status_code == 200
    resp = client.post("/reservations/order-1/release", headers=headers)

    assert resp.status_code == 404
    assert fake_db.items[0]["quantity"] == 8


def test_release_nonexistent_reservation_changes_no_stock(fake_db, tx):
    _seed_reservation(fake_db)
    client = TestClient(app)

    resp = client.post(
        "/reservations/no-such-order/release", headers={"X-Tenant-Id": "tenant-a"}
    )

    assert resp.status_code == 404
    assert fake_db.items[0]["quantity"] == 5
    assert len(fake_db.reservations) == 1
    assert "commit" not in tx.events
    assert not any(e.startswith("invalidate:") for e in tx.events)


def test_release_other_tenants_reservation_is_rejected(fake_db, tx):
    _seed_reservation(fake_db)  # order-1 belongs to tenant-a
    fake_db.add_item(sku="WIDGET", warehouse_id="w1", quantity=10, tenant_id="tenant-b")
    client = TestClient(app)

    resp = client.post(
        "/reservations/order-1/release", headers={"X-Tenant-Id": "tenant-b"}
    )

    assert resp.status_code == 404
    # tenant-a's reservation is untouched, and neither tenant's stock moved.
    assert fake_db.reservations == [
        {
            "order_id": "order-1",
            "tenant_id": "tenant-a",
            "sku": "WIDGET",
            "warehouse_id": "w1",
            "quantity": 3,
        }
    ]
    stock = {i["tenant_id"]: i["quantity"] for i in fake_db.items}
    assert stock == {"tenant-a": 5, "tenant-b": 10}
    assert "commit" not in tx.events
    assert not any(e.startswith("invalidate:") for e in tx.events)


# -- cache-correct release --------------------------------------------------
#
# These go through the real in-process cache (app.cache): nothing here patches
# cache.get / cache.put / cache.invalidate, so a stale entry left behind by the
# release path would be served by GET /items/{sku}/stock.


def _stock(client, sku, warehouse_id, tenant_id):
    resp = client.get(
        f"/items/{sku}/stock",
        params={"warehouse_id": warehouse_id},
        headers={"X-Tenant-Id": tenant_id},
    )
    assert resp.status_code == 200
    return resp.json()["quantity"]


def test_release_reservation_invalidates_cached_stock(client, fake_db):
    fake_db.add_item(sku="WIDGET", warehouse_id="w1", quantity=10, tenant_id="tenant-a")
    headers = {"X-Tenant-Id": "tenant-a"}
    reserve = client.post(
        "/items/reserve",
        json={
            "sku": "WIDGET",
            "warehouse_id": "w1",
            "quantity": 3,
            "order_id": "order-1",
        },
        headers=headers,
    )
    assert reserve.status_code == 200

    # Prime the cache with the reduced quantity.
    assert _stock(client, "WIDGET", "w1", "tenant-a") == 7
    assert cache_module.get(cache_module.stock_key("tenant-a", "w1", "WIDGET")) == 7

    resp = client.post("/reservations/order-1/release", headers=headers)
    assert resp.status_code == 200
    assert resp.json() == {"order_id": "order-1", "released": 3}

    # The next read sees the freshly restored quantity, not the cached 7.
    assert _stock(client, "WIDGET", "w1", "tenant-a") == 10


def test_release_reservation_leaves_other_skus_cached(client, fake_db):
    fake_db.add_item(sku="WIDGET", warehouse_id="w1", quantity=5, tenant_id="tenant-a")
    fake_db.add_item(sku="GADGET", warehouse_id="w1", quantity=4, tenant_id="tenant-a")
    fake_db.add_reservation(
        order_id="order-1",
        tenant_id="tenant-a",
        sku="WIDGET",
        warehouse_id="w1",
        quantity=3,
    )
    assert _stock(client, "WIDGET", "w1", "tenant-a") == 5
    assert _stock(client, "GADGET", "w1", "tenant-a") == 4

    resp = client.post(
        "/reservations/order-1/release", headers={"X-Tenant-Id": "tenant-a"}
    )
    assert resp.status_code == 200

    # Only the released reservation's SKU is invalidated.
    assert cache_module.get(cache_module.stock_key("tenant-a", "w1", "WIDGET")) is None
    assert cache_module.get(cache_module.stock_key("tenant-a", "w1", "GADGET")) == 4
    assert _stock(client, "WIDGET", "w1", "tenant-a") == 8


def test_failed_second_release_does_not_invalidate_cache(client, fake_db):
    _seed_reservation(fake_db)
    headers = {"X-Tenant-Id": "tenant-a"}
    assert (
        client.post("/reservations/order-1/release", headers=headers).status_code == 200
    )
    assert _stock(client, "WIDGET", "w1", "tenant-a") == 8

    # A no-op (404) second release leaves the fresh cache entry in place.
    assert (
        client.post("/reservations/order-1/release", headers=headers).status_code == 404
    )
    assert cache_module.get(cache_module.stock_key("tenant-a", "w1", "WIDGET")) == 8


def _seed_same_sku_elsewhere(fake_db):
    """Same SKU in another warehouse of tenant-a and in tenant-b's w1."""
    fake_db.add_item(sku="WIDGET", warehouse_id="w2", quantity=20, tenant_id="tenant-a")
    fake_db.add_item(sku="WIDGET", warehouse_id="w1", quantity=30, tenant_id="tenant-b")


def test_get_stock_cache_is_scoped_to_tenant_and_warehouse(client, fake_db):
    fake_db.add_item(sku="WIDGET", warehouse_id="w1", quantity=5, tenant_id="tenant-a")
    _seed_same_sku_elsewhere(fake_db)

    # Each read primes its own entry; none is served another's cached value.
    assert _stock(client, "WIDGET", "w1", "tenant-a") == 5
    assert _stock(client, "WIDGET", "w2", "tenant-a") == 20
    assert _stock(client, "WIDGET", "w1", "tenant-b") == 30
    assert _stock(client, "WIDGET", "w1", "tenant-a") == 5
    assert cache_module.get(cache_module.stock_key("tenant-a", "w1", "WIDGET")) == 5
    assert cache_module.get(cache_module.stock_key("tenant-a", "w2", "WIDGET")) == 20
    assert cache_module.get(cache_module.stock_key("tenant-b", "w1", "WIDGET")) == 30


def test_release_reservation_invalidates_only_its_tenant_and_warehouse(client, fake_db):
    fake_db.add_item(sku="WIDGET", warehouse_id="w1", quantity=10, tenant_id="tenant-a")
    _seed_same_sku_elsewhere(fake_db)
    reserve = client.post(
        "/items/reserve",
        json={
            "sku": "WIDGET",
            "warehouse_id": "w1",
            "quantity": 3,
            "order_id": "order-1",
        },
        headers={"X-Tenant-Id": "tenant-a"},
    )
    assert reserve.status_code == 200

    # Prime the cache for all three (tenant, warehouse) combinations.
    assert _stock(client, "WIDGET", "w1", "tenant-a") == 7
    assert _stock(client, "WIDGET", "w2", "tenant-a") == 20
    assert _stock(client, "WIDGET", "w1", "tenant-b") == 30

    resp = client.post(
        "/reservations/order-1/release", headers={"X-Tenant-Id": "tenant-a"}
    )
    assert resp.status_code == 200

    # Only the released reservation's tenant + warehouse + sku entry is gone.
    assert cache_module.get(cache_module.stock_key("tenant-a", "w1", "WIDGET")) is None
    assert cache_module.get(cache_module.stock_key("tenant-a", "w2", "WIDGET")) == 20
    assert cache_module.get(cache_module.stock_key("tenant-b", "w1", "WIDGET")) == 30
    assert _stock(client, "WIDGET", "w1", "tenant-a") == 10
    assert _stock(client, "WIDGET", "w2", "tenant-a") == 20
    assert _stock(client, "WIDGET", "w1", "tenant-b") == 30


def test_release_uses_reservation_warehouse_for_invalidation(client, fake_db):
    # The reservation is held in w2; the release request carries no warehouse,
    # so the key must come from the released row, not a default/other value.
    fake_db.add_item(sku="WIDGET", warehouse_id="w1", quantity=5, tenant_id="tenant-a")
    fake_db.add_item(sku="WIDGET", warehouse_id="w2", quantity=6, tenant_id="tenant-a")
    fake_db.add_reservation(
        order_id="order-2",
        tenant_id="tenant-a",
        sku="WIDGET",
        warehouse_id="w2",
        quantity=4,
    )
    assert _stock(client, "WIDGET", "w1", "tenant-a") == 5
    assert _stock(client, "WIDGET", "w2", "tenant-a") == 6

    resp = client.post(
        "/reservations/order-2/release", headers={"X-Tenant-Id": "tenant-a"}
    )
    assert resp.status_code == 200

    assert _stock(client, "WIDGET", "w2", "tenant-a") == 10
    assert cache_module.get(cache_module.stock_key("tenant-a", "w1", "WIDGET")) == 5


def test_stock_key_parts_cannot_collide():
    assert cache_module.stock_key("t:a", "w", "s") != cache_module.stock_key(
        "t", "a:w", "s"
    )
