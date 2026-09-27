import os

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


def _failing_stock_restore(fake_db, monkeypatch):
    real_execute = fake_db.execute

    def execute(sql, params=()):
        if sql.startswith("UPDATE items SET quantity = quantity + %s"):
            raise RuntimeError("stock restore failed")
        return real_execute(sql, params)

    monkeypatch.setattr(fake_db, "execute", execute)


def test_release_reservation_keeps_reservation_when_stock_restore_fails(
    fake_db, monkeypatch
):
    from fastapi.testclient import TestClient

    from app.main import app

    fake_db.add_item(sku="WIDGET", warehouse_id="w1", quantity=5, tenant_id="tenant-a")
    fake_db.add_reservation(
        order_id="order-1",
        tenant_id="tenant-a",
        sku="WIDGET",
        warehouse_id="w1",
        quantity=3,
    )
    _failing_stock_restore(fake_db, monkeypatch)

    client = TestClient(app, raise_server_exceptions=False)
    resp = client.post(
        "/reservations/order-1/release", headers={"X-Tenant-Id": "tenant-a"}
    )

    assert resp.status_code == 500
    assert fake_db.items[0]["quantity"] == 5
    assert [r["order_id"] for r in fake_db.reservations] == ["order-1"]


class _RecordingConnection:
    """A psycopg2-shaped connection that records statements and outcome."""

    def __init__(self, fail_on: str):
        self.fail_on = fail_on
        self.statements: list[str] = []
        self.committed = False
        self.rolled_back = False
        self.closed = False

    def cursor(self, cursor_factory=None):
        return self

    def execute(self, sql, params=()):
        if sql.startswith(self.fail_on):
            raise RuntimeError(f"failed: {self.fail_on}")
        self.statements.append(sql)

    def commit(self):
        self.committed = True

    def rollback(self):
        self.rolled_back = True

    def close(self):
        self.closed = True


def _release_with_real_transaction(fake_db, monkeypatch, fail_on):
    from fastapi.testclient import TestClient

    from app import db
    from app.main import app
    from app.routes import sync as sync_routes

    conn = _RecordingConnection(fail_on)
    monkeypatch.setattr(db, "get_connection", lambda: conn)
    monkeypatch.setattr(sync_routes, "transaction", db.transaction)
    fake_db.add_reservation(
        order_id="order-1",
        tenant_id="tenant-a",
        sku="WIDGET",
        warehouse_id="w1",
        quantity=3,
    )

    client = TestClient(app, raise_server_exceptions=False)
    resp = client.post(
        "/reservations/order-1/release", headers={"X-Tenant-Id": "tenant-a"}
    )
    return resp, conn


def test_release_reservation_rolls_back_when_stock_restore_fails(fake_db, monkeypatch):
    resp, conn = _release_with_real_transaction(
        fake_db, monkeypatch, fail_on="UPDATE items SET quantity = quantity + %s"
    )
    assert resp.status_code == 500
    assert conn.rolled_back and not conn.committed
    assert not any(s.startswith("DELETE FROM reservations") for s in conn.statements)


def test_release_reservation_rolls_back_restore_when_delete_fails(fake_db, monkeypatch):
    resp, conn = _release_with_real_transaction(
        fake_db, monkeypatch, fail_on="DELETE FROM reservations"
    )
    assert resp.status_code == 500
    # The stock restore ran on the same connection but was never committed.
    assert any(
        s.startswith("UPDATE items SET quantity = quantity + %s")
        for s in conn.statements
    )
    assert conn.rolled_back and not conn.committed
