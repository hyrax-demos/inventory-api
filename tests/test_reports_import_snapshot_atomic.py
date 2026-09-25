"""import_snapshot is all-or-nothing: every entry is applied, or none are."""

import psycopg2
import pytest

from app import db as db_module
from app.routes import reports as reports_routes

TENANT_A = {"X-Tenant-Id": "tenant-a"}


class _StagingConnection:
    """Lowest-layer fake psycopg2 connection with real transaction semantics.

    Writes are buffered and only reach the FakeDB on ``commit()``;
    ``rollback()`` discards them. ``fail_on`` makes the Nth write raise.
    """

    def __init__(self, fake_db, state):
        self._fake_db = fake_db
        self._state = state
        self._pending: list = []

    def cursor(self, cursor_factory=None):
        return _StagingCursor(self)

    def commit(self):
        for sql, params in self._pending:
            self._fake_db.execute(sql, params)
        self._pending = []

    def rollback(self):
        self._pending = []

    def close(self):
        pass


class _StagingCursor:
    def __init__(self, conn):
        self._conn = conn
        self.rowcount = 0

    def execute(self, sql, params=()):
        state = self._conn._state
        state["writes"] += 1
        if state["writes"] == state["fail_on"]:
            raise psycopg2.OperationalError("simulated write failure")
        self._conn._pending.append((sql, params))
        self.rowcount = 1


@pytest.fixture
def real_db_layer(fake_db, monkeypatch):
    """Route writes through the real app.db helpers over a staging connection."""
    state = {"writes": 0, "fail_on": None}
    monkeypatch.setattr(
        db_module, "get_connection", lambda: _StagingConnection(fake_db, state)
    )
    monkeypatch.setattr(reports_routes, "execute", db_module.execute, raising=False)
    monkeypatch.setattr(
        reports_routes, "transaction", db_module.transaction, raising=False
    )
    return state


def _seed(fake_db):
    for sku in ("A", "B", "C"):
        fake_db.add_item(sku=sku, warehouse_id="w1", quantity=1, tenant_id="tenant-a")


def _quantities(fake_db):
    return {r["sku"]: r["quantity"] for r in fake_db.items}


def test_malformed_last_entry_writes_nothing(client, fake_db, real_db_layer):
    _seed(fake_db)
    resp = client.post(
        "/reports/import",
        json={
            "items": [
                {"sku": "A", "warehouse_id": "w1", "quantity": 10},
                {"sku": "B", "warehouse_id": "w1", "quantity": 20},
                {"sku": "C", "warehouse_id": "w1", "quantity": "not-a-number"},
            ]
        },
        headers=TENANT_A,
    )
    assert resp.status_code == 400
    assert resp.json() == {"detail": "malformed snapshot entry"}
    assert _quantities(fake_db) == {"A": 1, "B": 1, "C": 1}
    assert real_db_layer["writes"] == 0


def test_write_failure_mid_batch_rolls_back_everything(client, fake_db, real_db_layer):
    _seed(fake_db)
    real_db_layer["fail_on"] = 2
    resp = client.post(
        "/reports/import",
        json={
            "items": [
                {"sku": "A", "warehouse_id": "w1", "quantity": 10},
                {"sku": "B", "warehouse_id": "w1", "quantity": 20},
                {"sku": "C", "warehouse_id": "w1", "quantity": 30},
            ]
        },
        headers=TENANT_A,
    )
    assert resp.status_code == 500
    assert _quantities(fake_db) == {"A": 1, "B": 1, "C": 1}


def test_valid_snapshot_applies_every_entry(client, fake_db, real_db_layer):
    _seed(fake_db)
    fake_db.add_item(sku="A", warehouse_id="w1", quantity=7, tenant_id="tenant-b")
    resp = client.post(
        "/reports/import",
        json={
            "items": [
                {"sku": "A", "warehouse_id": "w1", "quantity": 10},
                {"sku": "B", "warehouse_id": "w1", "quantity": 20},
                {"sku": "C", "warehouse_id": "w1", "quantity": 30},
            ]
        },
        headers=TENANT_A,
    )
    assert resp.status_code == 200
    assert resp.json() == {"items": 3, "snapshot": '{"received": 3}'}
    tenant_a = {
        r["sku"]: r["quantity"] for r in fake_db.items if r["tenant_id"] == "tenant-a"
    }
    assert tenant_a == {"A": 10, "B": 20, "C": 30}
    other = next(r for r in fake_db.items if r["tenant_id"] == "tenant-b")
    assert other["quantity"] == 7
