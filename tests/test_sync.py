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

    def execute(self, sql, params=()):
        if self._fail_on and sql.startswith(self._fail_on):
            raise RuntimeError(f"simulated DB failure on {self._fail_on!r}")
        self.rowcount = self._fake_db.execute(sql, params)


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
