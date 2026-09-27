"""Atomicity of POST /reservations/{order_id}/release.

The stock restore and the reservation-row delete must commit together or
not at all: if the restore fails, the reservation must survive (its stock is
still owed) and the error must reach the caller.
"""

import pytest
from fastapi.testclient import TestClient

from app import db as db_module
from app.main import app
from app.routes import sync as sync_routes

TENANT_A = {"X-Tenant-Id": "tenant-a"}
RESTORE_SQL_PREFIX = "UPDATE items SET quantity = quantity + %s"


def _seed(fake_db):
    fake_db.add_item(sku="WIDGET", warehouse_id="w1", quantity=5, tenant_id="tenant-a")
    fake_db.add_reservation(
        order_id="order-1",
        tenant_id="tenant-a",
        sku="WIDGET",
        warehouse_id="w1",
        quantity=3,
    )


@pytest.fixture
def lenient_client() -> TestClient:
    # Surface unhandled route errors as HTTP 500 responses instead of
    # re-raising them in the test, so we can assert on the failed request.
    return TestClient(app, raise_server_exceptions=False)


def test_release_restores_stock_and_deletes_reservation(client, fake_db):
    _seed(fake_db)

    resp = client.post("/reservations/order-1/release", headers=TENANT_A)

    assert resp.status_code == 200
    assert resp.json() == {"order_id": "order-1", "released": 3}
    assert fake_db.items[0]["quantity"] == 8
    assert fake_db.reservations == []


def test_release_keeps_reservation_when_restore_fails(
    lenient_client, fake_db, monkeypatch
):
    _seed(fake_db)
    real_execute = fake_db.execute

    def failing_execute(sql, params=()):
        if sql.startswith(RESTORE_SQL_PREFIX):
            raise RuntimeError("simulated stock-restore failure")
        return real_execute(sql, params)

    monkeypatch.setattr(fake_db, "execute", failing_execute)

    resp = lenient_client.post("/reservations/order-1/release", headers=TENANT_A)

    assert resp.status_code == 500
    # Reservation row survives and stock is unchanged.
    assert len(fake_db.reservations) == 1
    assert fake_db.reservations[0]["order_id"] == "order-1"
    assert fake_db.items[0]["quantity"] == 5


def test_release_restore_failure_propagates_to_caller(client, fake_db, monkeypatch):
    _seed(fake_db)
    real_execute = fake_db.execute

    def failing_execute(sql, params=()):
        if sql.startswith(RESTORE_SQL_PREFIX):
            raise RuntimeError("simulated stock-restore failure")
        return real_execute(sql, params)

    monkeypatch.setattr(fake_db, "execute", failing_execute)

    with pytest.raises(RuntimeError, match="simulated stock-restore failure"):
        client.post("/reservations/order-1/release", headers=TENANT_A)
    assert len(fake_db.reservations) == 1


class _RecordingConnection:
    """Lowest-layer fake psycopg2 connection that fails on the restore."""

    def __init__(self):
        self.statements: list[str] = []
        self.committed = False
        self.rolled_back = False
        self.closed = False

    def cursor(self, cursor_factory=None):
        return _RecordingCursor(self)

    def commit(self):
        self.committed = True

    def rollback(self):
        self.rolled_back = True

    def close(self):
        self.closed = True


class _RecordingCursor:
    def __init__(self, conn):
        self._conn = conn
        self.rowcount = 0

    def execute(self, sql, params=()):
        if sql.startswith(RESTORE_SQL_PREFIX):
            raise RuntimeError("simulated stock-restore failure")
        self._conn.statements.append(sql)
        self.rowcount = 1


def test_release_rolls_back_real_transaction_on_restore_failure(
    lenient_client, fake_db, monkeypatch
):
    """Using the real app.db.transaction(): the restore failure rolls back,
    never commits, and the DELETE is never issued."""
    _seed(fake_db)
    conns: list[_RecordingConnection] = []

    def fake_get_connection():
        conn = _RecordingConnection()
        conns.append(conn)
        return conn

    monkeypatch.setattr(db_module, "get_connection", fake_get_connection)
    monkeypatch.setattr(sync_routes, "transaction", db_module.transaction)
    # Any write that escaped the transaction would reach the fake store.
    monkeypatch.setattr(
        sync_routes,
        "execute",
        lambda *a, **k: pytest.fail("release must not write outside the transaction"),
    )

    resp = lenient_client.post("/reservations/order-1/release", headers=TENANT_A)

    assert resp.status_code == 500
    assert len(conns) == 1
    conn = conns[0]
    assert conn.rolled_back is True
    assert conn.committed is False
    assert conn.closed is True
    assert not any(s.startswith("DELETE FROM reservations") for s in conn.statements)
    assert len(fake_db.reservations) == 1
    assert fake_db.items[0]["quantity"] == 5
