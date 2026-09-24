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


# -- atomic release: stock restore + reservation delete share one transaction --


class _BufferedConnection:
    """psycopg2-like connection over a FakeDB that only applies writes on
    ``commit()`` and discards them on ``rollback()``, so the real
    ``app.db.transaction()`` helper can be exercised for atomicity."""

    def __init__(self, fake_db, fail_on: str):
        self._fake_db = fake_db
        self._fail_on = fail_on
        self.pending: list[tuple[str, tuple]] = []
        self.committed = False
        self.rolled_back = False

    def cursor(self, cursor_factory=None):
        return _BufferedCursor(self)

    def commit(self):
        for sql, params in self.pending:
            self._fake_db.execute(sql, params)
        self.pending.clear()
        self.committed = True

    def rollback(self):
        self.pending.clear()
        self.rolled_back = True

    def close(self):
        pass


class _BufferedCursor:
    def __init__(self, conn: _BufferedConnection):
        self._conn = conn
        self.rowcount = 0
        self._rows: list = []

    def execute(self, sql, params=()):
        if sql.startswith(self._conn._fail_on):
            raise RuntimeError(f"simulated failure: {self._conn._fail_on}")
        self._conn.pending.append((sql, params))
        if "RETURNING" in sql:
            # Preview the rows without touching the store; the write itself
            # is only applied on commit.
            self._rows = self._conn._fake_db.execute_returning(sql, params, apply=False)
            self.rowcount = len(self._rows)
        else:
            self.rowcount = 1

    def fetchone(self):
        return self._rows[0] if self._rows else None


def _seed_release(fake_db):
    fake_db.add_item(sku="WIDGET", warehouse_id="w1", quantity=5, tenant_id="tenant-a")
    fake_db.add_reservation(
        order_id="order-1",
        tenant_id="tenant-a",
        sku="WIDGET",
        warehouse_id="w1",
        quantity=3,
    )


def _use_real_transaction(monkeypatch, fake_db, fail_on: str) -> list:
    conns: list[_BufferedConnection] = []

    def get_connection():
        conn = _BufferedConnection(fake_db, fail_on)
        conns.append(conn)
        return conn

    monkeypatch.setattr(db_module, "get_connection", get_connection)
    monkeypatch.setattr(sync_routes, "transaction", db_module.transaction)
    return conns


def test_release_reservation_restores_stock_and_removes_row(client, fake_db):
    _seed_release(fake_db)
    resp = client.post(
        "/reservations/order-1/release", headers={"X-Tenant-Id": "tenant-a"}
    )
    assert resp.status_code == 200
    assert resp.json() == {"order_id": "order-1", "released": 3}
    assert fake_db.items[0]["quantity"] == 8
    assert fake_db.reservations == []


def test_release_reservation_commits_both_writes_in_one_transaction(
    monkeypatch, client, fake_db
):
    _seed_release(fake_db)
    conns = _use_real_transaction(monkeypatch, fake_db, fail_on="<never>")
    resp = client.post(
        "/reservations/order-1/release", headers={"X-Tenant-Id": "tenant-a"}
    )
    assert resp.status_code == 200
    assert len(conns) == 1 and conns[0].committed
    assert fake_db.items[0]["quantity"] == 8
    assert fake_db.reservations == []


@pytest.mark.parametrize(
    "fail_on",
    ["UPDATE items SET quantity = quantity + %s", "DELETE FROM reservations"],
    ids=["stock-restore-fails", "reservation-delete-fails"],
)
def test_release_reservation_failure_rolls_back_both(monkeypatch, fake_db, fail_on):
    _seed_release(fake_db)
    conns = _use_real_transaction(monkeypatch, fake_db, fail_on=fail_on)

    # The error must reach the client, not be swallowed.
    with pytest.raises(RuntimeError, match="simulated failure"):
        TestClient(app).post(
            "/reservations/order-1/release", headers={"X-Tenant-Id": "tenant-a"}
        )
    resp = TestClient(app, raise_server_exceptions=False).post(
        "/reservations/order-1/release", headers={"X-Tenant-Id": "tenant-a"}
    )
    assert resp.status_code == 500

    assert all(c.rolled_back and not c.committed for c in conns)
    assert len(fake_db.reservations) == 1
    assert fake_db.reservations[0]["order_id"] == "order-1"
    assert fake_db.items[0]["quantity"] == 5


# -- idempotent release: a second release never restores stock twice --


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


def test_release_unknown_reservation_is_404_and_changes_no_stock(client, fake_db):
    _seed_release(fake_db)
    resp = client.post(
        "/reservations/nope/release", headers={"X-Tenant-Id": "tenant-a"}
    )
    assert resp.status_code == 404
    assert fake_db.items[0]["quantity"] == 5
    assert len(fake_db.reservations) == 1


def test_release_other_tenants_reservation_is_404(client, fake_db):
    _seed_release(fake_db)
    resp = client.post(
        "/reservations/order-1/release", headers={"X-Tenant-Id": "tenant-b"}
    )
    assert resp.status_code == 404
    assert fake_db.items[0]["quantity"] == 5
    assert len(fake_db.reservations) == 1


def test_release_second_call_through_real_transaction_is_404(
    monkeypatch, client, fake_db
):
    _seed_release(fake_db)
    conns = _use_real_transaction(monkeypatch, fake_db, fail_on="<never>")
    headers = {"X-Tenant-Id": "tenant-a"}

    assert (
        client.post("/reservations/order-1/release", headers=headers).status_code == 200
    )
    second = client.post("/reservations/order-1/release", headers=headers)
    assert second.status_code == 404
    # The not-found path rolls back its (empty) transaction, never a 500.
    assert conns[0].committed
    assert conns[1].rolled_back and not conns[1].committed
    assert fake_db.items[0]["quantity"] == 8


def test_concurrent_releases_restore_stock_once(client, fake_db):
    # FakeDB's DELETE ... RETURNING is atomic (like a Postgres row lock), so
    # this checks the route lets the in-transaction DELETE decide, rather
    # than an earlier read that both threads could pass.
    from concurrent.futures import ThreadPoolExecutor

    _seed_release(fake_db)
    headers = {"X-Tenant-Id": "tenant-a"}

    def release(_):
        return client.post("/reservations/order-1/release", headers=headers).status_code

    with ThreadPoolExecutor(max_workers=8) as pool:
        codes = sorted(pool.map(release, range(8)))

    assert codes == [200] + [404] * 7
    assert fake_db.items[0]["quantity"] == 8
    assert fake_db.reservations == []
